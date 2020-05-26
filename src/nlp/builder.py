# coding=utf-8
# Copyright 2020 The HuggingFace NLP Authors and the TensorFlow Datasets Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""DatasetBuilder base class."""

import abc
import contextlib
import inspect
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from . import utils
from .arrow_reader import ArrowReader, ParquetReader, DatasetNotOnHfGcs
from .arrow_writer import ArrowWriter, BeamWriter
from .features import Features, Value
from .info import DATASET_INFOS_DICT_FILE_NAME, DatasetInfo, DatasetInfosDict, DATASET_INFO_FILENAME, LICENSE_FILENAME
from .naming import camelcase_to_snakecase, filename_prefix_for_split
from .splits import Split, SplitDict
from .utils.download_manager import DownloadManager, GenerateMode
from .utils.file_utils import HF_DATASETS_CACHE, DownloadConfig, is_remote_url
from .utils.info_utils import verify_checksums, verify_splits


logger = logging.getLogger(__name__)

FORCE_REDOWNLOAD = GenerateMode.FORCE_REDOWNLOAD
REUSE_CACHE_IF_EXISTS = GenerateMode.REUSE_CACHE_IF_EXISTS
REUSE_DATASET_IF_EXISTS = GenerateMode.REUSE_DATASET_IF_EXISTS


@dataclass
class BuilderConfig:
    """Base class for `DatasetBuilder` data configuration.

    DatasetBuilder subclasses with data configuration options should subclass
    `BuilderConfig` and add their own properties.
    """

    name: str = "default"
    version: Optional[Union[str, utils.Version]] = "0.0.0"
    data_dir: str = None
    data_files: Union[Dict, List] = None
    description: str = None


class DatasetBuilder:
    """Abstract base class for all datasets.

    `DatasetBuilder` has 3 key methods:

        * `nlp.DatasetBuilder.info`: documents the dataset, including feature
            names, types, and shapes, version, splits, citation, etc.
        * `nlp.DatasetBuilder.download_and_prepare`: downloads the source data
            and writes it to disk.
        * `nlp.DatasetBuilder.as_dataset`: generate an `Dataset`.

    **Configuration**: Some `DatasetBuilder`s expose multiple variants of the
    dataset by defining a `nlp.BuilderConfig` subclass and accepting a
    config object (or name) on construction. Configurable datasets expose a
    pre-defined set of configurations in `nlp.DatasetBuilder.builder_configs`.
    """

    # Default version.
    VERSION = utils.Version("0.0.0")

    # Class for the builder config.
    BUILDER_CONFIG_CLASS = BuilderConfig

    # Named configurations that modify the data generated by download_and_prepare.
    BUILDER_CONFIGS = []

    # Must be set for datasets that use 'manual_dir' functionality - the ones
    # that require users to do additional steps to download the data
    # (this is usually due to some external regulations / rules).
    #
    # This field should contain a string with user instructions, including
    # the list of files that should be present. It will be
    # displayed in the dataset documentation.
    MANUAL_DOWNLOAD_INSTRUCTIONS = None

    def __init__(
        self, cache_dir=None, name=None, **config_kwargs,
    ):
        """Constructs a DatasetBuilder.

        Callers must pass arguments as keyword arguments.

        Args:
            cache_dir: `str`, directory to read/write data. Defaults to "~/nlp".
            name: `str` name, optional configuration for the dataset that affects the data generated on disk. Different
                `builder_config`s will have their own subdirectories and versions.
                If not provided, uses the first configuration in self.BUILDER_CONFIGS
            config_kwargs: will override the defaults kwargs in config

        """
        # DatasetBuilder name
        self.name = camelcase_to_snakecase(self.__class__.__name__)

        # Prepare config: DatasetConfig contains name, version and description but can be extended by each dataset
        config_kwargs = dict((key, value) for key, value in config_kwargs.items() if value is not None)
        self.config = self._create_builder_config(name, **config_kwargs,)

        # prepare info: DatasetInfo are a standardized dataclass across all datasets
        # Prefill datasetinfo
        info = self.get_exported_dataset_info()
        info.update(self._info())
        info.builder_name = self.name
        info.config_name = self.config.name
        info.version = self.config.version
        self.info = info

        # prepare data dirs
        self._cache_dir_root = os.path.expanduser(cache_dir or HF_DATASETS_CACHE)
        self._cache_dir = self._build_cache_dir()
        if os.path.exists(self._cache_dir):
            logger.info("Overwrite dataset info from restored data version.")
            self.info = DatasetInfo.from_directory(self._cache_dir)

    @property
    def does_require_manual_download(self):
        return hasattr(self, "MANUAL_DOWNLOAD_INSTRUCTIONS")

    @classmethod
    def get_all_exported_dataset_infos(cls) -> dict:
        """Empty dict if doesn't exist"""
        dset_infos_file_path = os.path.join(cls.get_imported_module_dir(), DATASET_INFOS_DICT_FILE_NAME)
        if os.path.exists(dset_infos_file_path):
            return DatasetInfosDict.from_directory(cls.get_imported_module_dir())
        return {}

    def get_exported_dataset_info(self) -> DatasetInfo:
        """Empty DatasetInfo if doesn't exist"""
        return self.get_all_exported_dataset_infos().get(self.config.name, DatasetInfo())

    def _create_builder_config(self, name=None, **config_kwargs):
        """ Create and validate BuilderConfig object.
            Uses the first configuration in self.BUILDER_CONFIGS if name is None
            config_kwargs override the defaults kwargs in config
        """
        builder_config = None
        if name is None and self.BUILDER_CONFIGS:
            builder_config = self.BUILDER_CONFIGS[0]
            logger.info("No config specified, defaulting to first: %s/%s", self.name, builder_config.name)
        if isinstance(name, str):
            builder_config = self.builder_configs.get(name)
            if builder_config is None and self.BUILDER_CONFIGS:
                raise ValueError(
                    "BuilderConfig %s not found. Available: %s" % (name, list(self.builder_configs.keys()))
                )
        if not builder_config:
            if name is not None:
                config_kwargs["name"] = name
            if "version" not in config_kwargs and hasattr(self, "VERSION") and self.VERSION:
                config_kwargs["version"] = self.VERSION
            builder_config = self.BUILDER_CONFIG_CLASS(**config_kwargs)

        for key, value in config_kwargs.items():
            if value is not None:
                setattr(builder_config, key, value)

        name = builder_config.name
        if not name:
            raise ValueError("BuilderConfig must have a name, got %s" % name)
        is_custom = name not in self.builder_configs
        if is_custom:
            logger.warning("Using custom data configuration %s", name)
        else:
            if builder_config is not self.builder_configs[name]:
                raise ValueError(
                    "Cannot name a custom BuilderConfig the same as an available "
                    "BuilderConfig. Change the name. Available BuilderConfigs: %s"
                    % (list(self.builder_configs.keys()))
                )
            if not builder_config.version:
                raise ValueError("BuilderConfig %s must have a version" % name)
            # if not builder_config.description:
            #     raise ValueError("BuilderConfig %s must have a description" % name)
        return builder_config

    @utils.classproperty
    @classmethod
    @utils.memoize()
    def builder_configs(cls):
        """Pre-defined list of configurations for this builder class."""
        config_dict = {config.name: config for config in cls.BUILDER_CONFIGS}
        if len(config_dict) != len(cls.BUILDER_CONFIGS):
            names = [config.name for config in cls.BUILDER_CONFIGS]
            raise ValueError("Names in BUILDER_CONFIGS must not be duplicated. Got %s" % names)
        return config_dict

    @property
    def cache_dir(self):
        return self._cache_dir

    def _relative_data_dir(self, with_version=True):
        """Relative path of this dataset in cache_dir."""
        builder_data_dir = self.name
        builder_config = self.config
        if builder_config:
            builder_data_dir = os.path.join(builder_data_dir, builder_config.name)
        if not with_version:
            return builder_data_dir

        version = self.config.version
        version_data_dir = os.path.join(builder_data_dir, str(version))
        return version_data_dir

    def _build_cache_dir(self):
        """Return the data directory for the current version."""
        builder_data_dir = os.path.join(self._cache_dir_root, self._relative_data_dir(with_version=False))
        version_data_dir = os.path.join(self._cache_dir_root, self._relative_data_dir(with_version=True))

        def _other_versions_on_disk():
            """Returns previous versions on disk."""
            if not os.path.exists(builder_data_dir):
                return []

            version_dirnames = []
            for dir_name in os.listdir(builder_data_dir):
                try:
                    version_dirnames.append((utils.Version(dir_name), dir_name))
                except ValueError:  # Invalid version (ex: incomplete data dir)
                    pass
            version_dirnames.sort(reverse=True)
            return version_dirnames

        # Check and warn if other versions exist on disk
        version_dirs = _other_versions_on_disk()
        if version_dirs:
            other_version = version_dirs[0][0]
            if other_version != self.config.version:
                warn_msg = (
                    "Found a different version {other_version} of dataset {name} in "
                    "cache_dir {cache_dir}. Using currently defined version "
                    "{cur_version}.".format(
                        other_version=str(other_version),
                        name=self.name,
                        cache_dir=self._cache_dir_root,
                        cur_version=str(self.config.version),
                    )
                )
                logger.warning(warn_msg)

        return version_data_dir

    @abc.abstractmethod
    def _info(self) -> DatasetInfo:
        """Construct the DatasetInfo object. See `DatasetInfo` for details.

        Warning: This function is only called once and the result is cached for all
        following .info() calls.

        Returns:
            info: (DatasetInfo) The dataset information
        """
        raise NotImplementedError

    @classmethod
    def get_imported_module_dir(cls):
        """Return the path of the module of this class or subclass."""
        return os.path.dirname(inspect.getfile(inspect.getmodule(cls)))

    def download_and_prepare(
        self,
        download_config: Optional[DownloadConfig] = None,
        download_mode: Optional[GenerateMode] = None,
        ignore_verifications: bool = False,
        save_infos: bool = False,
        try_from_hf_gcs: bool = True,
        dl_manager: Optional[DownloadManager] = None,
        **download_and_prepare_kwargs,
    ):
        """Downloads and prepares dataset for reading.

        Args:
            download_config (Optional ``nlp.DownloadConfig``: specific download configuration parameters.
            download_mode (Optional `nlp.GenerateMode`): select the download/generate mode - Default to REUSE_DATASET_IF_EXISTS
            ignore_verifications (bool): Ignore the verifications of the downloaded/processed dataset information (checksums/size/splits/...)
            save_infos (bool): Save the dataset information (checksums/size/splits/...)
            dl_manager (Optional ``nlp.DownloadManager``): specific Download Manger to use
        """
        download_mode = GenerateMode(download_mode or GenerateMode.REUSE_DATASET_IF_EXISTS)

        data_exists = os.path.exists(self._cache_dir)
        if data_exists and download_mode == REUSE_DATASET_IF_EXISTS:
            logger.info("Reusing dataset %s (%s)", self.name, self._cache_dir)
            return

        # Currently it's not possible to overwrite the data because it would
        # conflict with versioning: If the last version has already been generated,
        # it will always be reloaded and cache_dir will be set at construction.
        if data_exists and download_mode != REUSE_CACHE_IF_EXISTS:
            raise ValueError(
                "Trying to overwrite an existing dataset {} at {}. A dataset with "
                "the same version {} already exists. If the dataset has changed, "
                "please update the version number.".format(self.name, self._cache_dir, self.config.version)
            )

        logger.info("Generating dataset %s (%s)", self.name, self._cache_dir)
        if not is_remote_url(self._cache_dir):  # if cache dir is local, check for available space
            os.makedirs(self._cache_dir, exist_ok=True)
            if not utils.has_sufficient_disk_space(self.info.size_in_bytes or 0, directory=self._cache_dir_root):
                raise IOError(
                    "Not enough disk space. Needed: {} (download: {}, generated: {})".format(
                        utils.size_str(self.info.size_in_bytes or 0),
                        utils.size_str(self.info.download_size or 0),
                        utils.size_str(self.info.dataset_size or 0),
                    )
                )

        # Try to download the already prepared dataset files
        if try_from_hf_gcs:
            try:
                reader = ArrowReader(self._cache_dir, self.info)
                reader.download_from_hf_gcs(self.cache_dir, self._relative_data_dir(with_version=True))
                downloaded_info = DatasetInfo.from_directory(self._cache_dir)
                self.info.update(downloaded_info)
                logger.info("Dataset downloaded from Hf google storage.")
                print(
                    f"Dataset {self.name} downloaded and prepared to {self._cache_dir}. "
                    f"Subsequent calls will reuse this data."
                )
                return
            except DatasetNotOnHfGcs:
                logger.info("Dataset not on Hf google storage. Downloading and preparing it from source")

        # Print is intentional: we want this to always go to stdout so user has
        # information needed to cancel download/preparation if needed.
        # This comes right before the progress bar.
        print(
            f"Downloading and preparing dataset {self.info.builder_name}/{self.info.config_name} "
            f"(download: {utils.size_str(self.info.download_size)}, generated: {utils.size_str(self.info.dataset_size)}, "
            f"total: {utils.size_str(self.info.size_in_bytes)}) to {self._cache_dir}..."
        )

        if dl_manager is None:
            if download_config is None:
                download_config = DownloadConfig()
                download_config.cache_dir = os.path.join(self._cache_dir_root, "downloads")
                download_config.force_download = download_mode == FORCE_REDOWNLOAD

            dl_manager = DownloadManager(
                dataset_name=self.name, download_config=download_config, data_dir=self.config.data_dir
            )

        @contextlib.contextmanager
        def incomplete_dir(dirname):
            """Create temporary dir for dirname and rename on exit."""
            if is_remote_url(dirname):
                yield dirname
            else:
                tmp_dir = dirname + ".incomplete"
                os.makedirs(tmp_dir)
                try:
                    yield tmp_dir
                    if os.path.isdir(dirname):
                        shutil.rmtree(dirname)
                    os.rename(tmp_dir, dirname)
                finally:
                    if os.path.exists(tmp_dir):
                        shutil.rmtree(tmp_dir)

        # Create a tmp dir and rename to self._cache_dir on successful exit.
        with incomplete_dir(self._cache_dir) as tmp_data_dir:
            # Temporarily assign _cache_dir to tmp_data_dir to avoid having to forward
            # it to every sub function.
            with utils.temporary_assignment(self, "_cache_dir", tmp_data_dir):
                verify_infos = not save_infos and not ignore_verifications
                self._download_and_prepare(
                    dl_manager=dl_manager, verify_infos=verify_infos, **download_and_prepare_kwargs
                )
                # Sync info
                self.info.dataset_size = sum(split.num_bytes for split in self.info.splits.values())
                self.info.download_checksums = dl_manager.get_recorded_sizes_checksums()
                self.info.size_in_bytes = self.info.dataset_size + self.info.download_size
                # Save info
                self._save_info()

        # Save to datasetinfos
        if save_infos:
            DatasetInfosDict(**{self.config.name: self.info}).write_to_directory(self.get_imported_module_dir())

        print(
            f"Dataset {self.name} downloaded and prepared to {self._cache_dir}. "
            f"Subsequent calls will reuse this data."
        )

    def _download_and_prepare(self, dl_manager, verify_infos, **prepare_split_kwargs):
        """Downloads and prepares dataset for reading.

        This is the internal implementation to overwrite called when user calls
        `download_and_prepare`. It should download all required data and generate
        the pre-processed datasets files.

        Args:
            dl_manager: (DownloadManager) `DownloadManager` used to download and cache
                data.
            verify_infos: bool, if True, do not perform checksums and size tests.
            prepare_split_kwargs: Additional options.
        """
        if not is_remote_url(self._cache_dir):
            os.makedirs(self._cache_dir, exist_ok=True)

        # Generating data for all splits
        split_dict = SplitDict(dataset_name=self.name)
        split_generators_kwargs = self._make_split_generators_kwargs(prepare_split_kwargs)
        split_generators = self._split_generators(dl_manager, **split_generators_kwargs)
        # Checksums verification
        if verify_infos:
            verify_checksums(self.info.download_checksums, dl_manager.get_recorded_sizes_checksums())
        for split_generator in split_generators:
            if str(split_generator.split_info.name).lower() == "all":
                raise ValueError(
                    "`all` is a special split keyword corresponding to the "
                    "union of all splits, so cannot be used as key in "
                    "._split_generator()."
                )

            logger.info("Generating split %s", split_generator.split_info.name)
            split_dict.add(split_generator.split_info)

            try:
                # Prepare split will record examples associated to the split
                self._prepare_split(split_generator, **prepare_split_kwargs)
            except OSError:
                raise OSError("Cannot find data file. " + (self.MANUAL_DOWNLOAD_INSTRUCTIONS or ""))

        if verify_infos:
            verify_splits(self.info.splits, split_dict)
        # Update the info object with the splits.
        self.info.splits = split_dict
        self.info.download_size = dl_manager.downloaded_size

    def _save_info(self):
        self.info.write_to_directory(self._cache_dir)

    def _make_split_generators_kwargs(self, prepare_split_kwargs):
        """Get kwargs for `self._split_generators()` from `prepare_split_kwargs`."""
        del prepare_split_kwargs
        return {}

    def as_dataset(self, split: Optional[Split] = None):
        """ Return a Dataset for the specified split.
        """
        logger.info("Constructing Dataset for split %s, from %s", split, self._cache_dir)
        if not os.path.exists(self._cache_dir):
            raise AssertionError(
                (
                    "Dataset %s: could not find data in %s. Please make sure to call "
                    "builder.download_and_prepare(), or pass download=True to "
                    "nlp.load_dataset() before trying to access the Dataset object."
                )
                % (self.name, self._cache_dir_root)
            )

        # By default, return all splits
        if split is None:
            split = {s: s for s in self.info.splits}

        # Create a dataset for each of the given splits
        datasets = utils.map_nested(self._build_single_dataset, split, map_tuple=True)
        return datasets

    def _build_single_dataset(self, split):
        """as_dataset for a single split."""
        if isinstance(split, str):
            split = Split(split)

        # Build base dataset
        ds = self._as_dataset(split=split,)
        return ds

    def _as_dataset(self, split: Split = Split.TRAIN):
        """Constructs a `Dataset`.

        This is the internal implementation to overwrite called when user calls
        `as_dataset`. It should read the pre-processed datasets files and generate
        the `Dataset` object.

        Args:
            split: `nlp.Split` which subset of the data to read.

        Returns:
            `Dataset`
        """

        ds = ArrowReader(self._cache_dir, self.info).read(
            name=self.name, instructions=split, split_infos=self.info.splits.values(),
        )
        return ds

    @abc.abstractmethod
    def _split_generators(self, dl_manager):
        """Specify feature dictionary generators and dataset splits.

        This function returns a list of `SplitGenerator`s defining how to generate
        data and what splits to use.

        Example:

            return[
                    nlp.SplitGenerator(
                            name=nlp.Split.TRAIN,
                            gen_kwargs={'file': 'train_data.zip'},
                    ),
                    nlp.SplitGenerator(
                            name=nlp.Split.TEST,
                            gen_kwargs={'file': 'test_data.zip'},
                    ),
            ]

        The above code will first call `_generate_examples(file='train_data.zip')`
        to write the train data, then `_generate_examples(file='test_data.zip')` to
        write the test data.

        Datasets are typically split into different subsets to be used at various
        stages of training and evaluation.

        Note that for datasets without a `VALIDATION` split, you can use a
        fraction of the `TRAIN` data for evaluation as you iterate on your model
        so as not to overfit to the `TEST` data.

        For downloads and extractions, use the given `download_manager`.
        Note that the `DownloadManager` caches downloads, so it is fine to have each
        generator attempt to download the source data.

        A good practice is to download all data in this function, and then
        distribute the relevant parts to each split with the `gen_kwargs` argument

        Args:
            dl_manager: (DownloadManager) Download manager to download the data

        Returns:
            `list<SplitGenerator>`.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def _prepare_split(self, split_generator, **kwargs):
        """Generate the examples and record them on disk.

        Args:
            split_generator: `SplitGenerator`, Split generator to process
            **kwargs: Additional kwargs forwarded from _download_and_prepare (ex:
                beam pipeline)
        """
        raise NotImplementedError()


class GeneratorBasedBuilder(DatasetBuilder):
    """Base class for datasets with data generation based on dict generators.

    `GeneratorBasedBuilder` is a convenience class that abstracts away much
    of the data writing and reading of `DatasetBuilder`. It expects subclasses to
    implement generators of feature dictionaries across the dataset splits
    (`_split_generators`). See the method docstrings for details.
    """

    def __init__(self, *args, **kwargs):
        super(GeneratorBasedBuilder, self).__init__(*args, **kwargs)
        self._writer_batch_size = kwargs.get("writer_batch_size")

    @abc.abstractmethod
    def _generate_examples(self, **kwargs):
        """Default function generating examples for each `SplitGenerator`.

        This function preprocess the examples from the raw data to the preprocessed
        dataset files.
        This function is called once for each `SplitGenerator` defined in
        `_split_generators`. The examples yielded here will be written on
        disk.

        Args:
            **kwargs: `dict`, Arguments forwarded from the SplitGenerator.gen_kwargs

        Yields:
            key: `str` or `int`, a unique deterministic example identification key.
                * Unique: An error will be raised if two examples are yield with the
                    same key.
                * Deterministic: When generating the dataset twice, the same example
                    should have the same key.
                Good keys can be the image id, or line number if examples are extracted
                from a text file.
                The key will be hashed and sorted to shuffle examples deterministically,
                such as generating the dataset multiple times keep examples in the
                same order.
            example: `dict<str feature_name, feature_value>`, a feature dictionary
                ready to be encoded and written to disk. The example will be
                encoded with `self.info.features.encode_example({...})`.
        """
        raise NotImplementedError()

    def _prepare_split(self, split_generator):
        split_info = split_generator.split_info

        fname = "{}-{}.arrow".format(self.name, split_generator.name)
        fpath = os.path.join(self._cache_dir, fname)
        examples_type = self.info.features.type
        writer = ArrowWriter(data_type=examples_type, path=fpath, writer_batch_size=self._writer_batch_size)

        generator = self._generate_examples(**split_generator.gen_kwargs)
        for key, record in utils.tqdm(generator, unit=" examples", total=split_info.num_examples, leave=False):
            example = self.info.features.encode_example(record)
            writer.write(example)
        num_examples, num_bytes = writer.finalize()

        assert num_examples == num_examples, f"Expected to write {split_info.num_examples} but wrote {num_examples}"
        split_generator.split_info.num_examples = num_examples
        split_generator.split_info.num_bytes = num_bytes


class ArrowBasedBuilder(DatasetBuilder):
    """Base class for datasets with data generation based on Arrow loading functions (CSV/JSON/Parquet).

    """

    @abc.abstractmethod
    def _generate_examples(self, **kwargs):
        """Default function generating examples for each `SplitGenerator`.

        This function preprocess the examples from the raw data to the preprocessed
        dataset files.
        This function is called once for each `SplitGenerator` defined in
        `_split_generators`. The examples yielded here will be written on
        disk.

        Args:
            **kwargs: `dict`, Arguments forwarded from the SplitGenerator.gen_kwargs

        Yields:
            key: `str` or `int`, a unique deterministic example identification key.
                * Unique: An error will be raised if two examples are yield with the
                    same key.
                * Deterministic: When generating the dataset twice, the same example
                    should have the same key.
                Good keys can be the image id, or line number if examples are extracted
                from a text file.
                The key will be hashed and sorted to shuffle examples deterministically,
                such as generating the dataset multiple times keep examples in the
                same order.
            example: `dict<str feature_name, feature_value>`, a feature dictionary
                ready to be encoded and written to disk. The example will be
                encoded with `self.info.features.encode_example({...})`.
        """
        raise NotImplementedError()

    def _prepare_split(self, split_generator):
        fname = "{}-{}.arrow".format(self.name, split_generator.name)
        fpath = os.path.join(self._cache_dir, fname)

        writer = ArrowWriter(path=fpath)

        generator = self._generate_tables(**split_generator.gen_kwargs)
        for key, table in utils.tqdm(generator, unit=" tables", leave=False):
            writer.write_table(table)
        num_examples, num_bytes = writer.finalize()

        split_generator.split_info.num_examples = num_examples
        split_generator.split_info.num_bytes = num_bytes
        self.info.features = Features(
            {
                field.name: Value(str(field.type)) for field in writer.schema
            }  # TODO have nested conversion from Arrow to Python
        )


class BeamBasedBuilder(DatasetBuilder):
    """Beam based Builder."""

    def __init__(self, *args, **kwargs):
        super(BeamBasedBuilder, self).__init__(*args, **kwargs)
        self._beam_runner = kwargs.get("beam_runner")
        self._beam_options = kwargs.get("beam_options")
        self._beam_writers = {}  # {split: beam_writer} mapping.

    def _make_split_generators_kwargs(self, prepare_split_kwargs):
        # Pass `pipeline` into `_split_generators()` from `prepare_split_kwargs` if
        # it's in the call signature of `_split_generators()`.
        # This allows for global preprocessing in beam.
        split_generators_kwargs = {}
        split_generators_arg_names = inspect.signature(self._split_generators).parameters.keys()
        if "pipeline" in split_generators_arg_names:
            split_generators_kwargs["pipeline"] = prepare_split_kwargs["pipeline"]
        return split_generators_kwargs

    @abc.abstractmethod
    def _build_pcollection(self, pipeline, **kwargs):
        """Build the beam pipeline examples for each `SplitGenerator`.

        This function extracts examples from the raw data with parallel transforms
        in a Beam pipeline. It is called once for each `SplitGenerator` defined in
        `_split_generators`. The examples from the PCollection will be
        encoded and written to disk.

        Warning: When running in a distributed setup, make sure that the data
        which will be read (download_dir, manual_dir,...) and written (cache_dir)
        can be accessed by the workers jobs. The data should be located in a
        shared filesystem, like GCS.

        Example:

        ```
        def _build_pcollection(pipeline, extracted_dir):
            return (
                    pipeline
                    | beam.Create(gfile.io.listdir(extracted_dir))
                    | beam.Map(_process_file)
            )
        ```

        Args:
            pipeline: `beam.Pipeline`, root Beam pipeline
            **kwargs: Arguments forwarded from the SplitGenerator.gen_kwargs

        Returns:
            pcollection: `PCollection`, an Apache Beam PCollection containing the
                example to send to `self.info.features.encode_example(...)`.
        """
        raise NotImplementedError()

    def _as_dataset(self, split: Split = Split.TRAIN):
        """Constructs a `Dataset`.

        This is the internal implementation to overwrite called when user calls
        `as_dataset`. It should read the pre-processed datasets files and generate
        the `Dataset` object.

        Args:
            split: `nlp.Split` which subset of the data to read.

        Returns:
            `Dataset`
        """

        ds = ParquetReader(self._cache_dir, self.info).read(
            name=self.name, instructions=split, split_infos=self.info.splits.values(),
        )
        return ds

    def _download_and_prepare(self, dl_manager, verify_infos):
        # Create the Beam pipeline and forward it to _prepare_split
        import apache_beam as beam
        import nlp.utils.beam_utils as beam_utils

        beam_runner = self._beam_runner
        beam_options = self._beam_options

        if not beam_runner and not beam_options:
            raise ValueError(
                "Trying to generate a dataset using Apache Beam, yet no Beam Runner "
                "or PipelineOptions() has been provided in `load_dataset` or in the "
                "builder arguments. For big datasets it has to run on large-scale data "
                "processing tools like Dataflow, Spark, etc. More information about "
                "Apache Beam runners at "
                "https://beam.apache.org/documentation/runners/capability-matrix/"
            )

        beam_options = beam_options or beam.options.pipeline_options.PipelineOptions()
        # Beam type checking assumes transforms multiple outputs are of same type,
        # which is not our case. Plus it doesn't handle correctly all types, so we
        # are better without it.
        beam_options.view_as(beam.options.pipeline_options.TypeOptions).pipeline_type_check = False
        # Use a single pipeline for all splits
        pipeline = beam_utils.BeamPipeline(runner=beam_runner, options=beam_options,)
        super(BeamBasedBuilder, self)._download_and_prepare(
            dl_manager, verify_infos=False, pipeline=pipeline,
        )  # TODO handle verify_infos in beam datasets
        # Run pipeline
        pipeline_results = pipeline.run()
        pipeline_results.wait_until_finish()
        metrics = pipeline_results.metrics()
        # Update `info.splits`.
        split_dict = self.info.splits
        for split_name, beam_writer in self._beam_writers.items():
            m_filter = beam.metrics.MetricsFilter().with_namespace(namespace=split_name)
            num_examples, num_bytes = beam_writer.finalize(metrics.query(m_filter))
            split_info = split_dict[split_name]
            split_info.num_examples = num_examples
            split_info.num_bytes = num_bytes

    def _save_info(self):
        import apache_beam as beam

        fs = beam.io.filesystems.FileSystems
        with fs.create(os.path.join(self._cache_dir, DATASET_INFO_FILENAME)) as f:
            self.info._dump_info(f)
        with fs.create(os.path.join(self._cache_dir, LICENSE_FILENAME)) as f:
            self.info._dump_license(f)

    def _prepare_split(self, split_generator, pipeline):
        import apache_beam as beam

        split_name = split_generator.split_info.name
        output_prefix = filename_prefix_for_split(self.name, split_name)
        output_prefix = os.path.join(self._cache_dir, output_prefix)

        # To write examples to disk:
        fname = "{}-{}.arrow".format(self.name, split_name)
        fpath = os.path.join(self._cache_dir, fname)
        examples_type = self.info.features.type
        beam_writer = BeamWriter(examples_type, path=fpath, namespace=split_name)
        self._beam_writers[split_name] = beam_writer

        encode_example = self.info.features.encode_example

        # Note: We need to wrap the pipeline in a PTransform to avoid re-using the
        # same label names for each split
        @beam.ptransform_fn
        def _build_pcollection(pipeline):
            """PTransformation which build a single split."""
            # Encode the PCollection
            pcoll_examples = self._build_pcollection(pipeline, **split_generator.gen_kwargs)
            pcoll_examples |= "Encode" >> beam.Map(lambda key_ex: (key_ex[0], encode_example(key_ex[1])))
            return beam_writer.write_from_pcollection(pcoll_examples)

        # Add the PCollection to the pipeline
        _ = pipeline | split_name >> _build_pcollection()  # pylint: disable=no-value-for-parameter
