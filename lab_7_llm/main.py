"""
Laboratory work.

Working with Large Language Models.
"""
# pylint: disable=too-few-public-methods, undefined-variable, too-many-arguments, super-init-not-called
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import torch
import torch.nn.functional as F
from datasets import load_dataset
from evaluate import load
from pandas import DataFrame
from torch.utils.data import DataLoader, Dataset
from torchinfo import summary
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from core_utils.llm.llm_pipeline import AbstractLLMPipeline
from core_utils.llm.metrics import Metrics
from core_utils.llm.raw_data_importer import AbstractRawDataImporter
from core_utils.llm.raw_data_preprocessor import AbstractRawDataPreprocessor, ColumnNames
from core_utils.llm.task_evaluator import AbstractTaskEvaluator
from core_utils.llm.time_decorator import report_time


class RawDataImporter(AbstractRawDataImporter):
    """
    A class that imports the HuggingFace dataset.
    """

    @report_time
    def obtain(self) -> None:
        """
        Download a dataset.

        Raises:
            TypeError: In case of downloaded dataset is not pd.DataFrame
        """
        self._raw_data = load_dataset(self._hf_name, split="test").to_pandas()


class RawDataPreprocessor(AbstractRawDataPreprocessor):
    """
    A class that analyzes and preprocesses a dataset.
    """

    def analyze(self) -> dict:
        """
        Analyze a dataset.

        Returns:
            dict: Dataset key properties
        """
        props = {
            'dataset_number_of_samples': self._raw_data.shape[0],
            'dataset_columns': self._raw_data.shape[1],
            'dataset_duplicates': self._raw_data.duplicated().sum(),
            'dataset_empty_rows': self._raw_data.isnull().any(axis=1).sum(),
            'dataset_sample_min_len': self._raw_data['text'].dropna().str.len().min(),
            'dataset_sample_max_len': self._raw_data['text'].dropna().str.len().max()
        }
        return props

    @report_time
    def transform(self) -> None:
        """
        Apply preprocessing transformations to the raw dataset.
        """
        self._data = self._raw_data.copy()
        self._data.rename(columns={
            'label': 'target',
            'text': 'source'
        }, inplace=True)
        self._data.reset_index(drop=True, inplace=True)


class TaskDataset(Dataset):
    """
    A class that converts pd.DataFrame to Dataset and works with it.
    """

    def __init__(self, data: pd.DataFrame) -> None:
        """
        Initialize an instance of TaskDataset.

        Args:
            data (pandas.DataFrame): Original data
        """
        self._data = data

    def __len__(self) -> int:
        """
        Return the number of items in the dataset.

        Returns:
            int: The number of items in the dataset
        """
        return self._data.shape[0]

    def __getitem__(self, index: int) -> tuple[str, ...]:
        """
        Retrieve an item from the dataset by index.

        Args:
            index (int): Index of sample in dataset

        Returns:
            tuple[str, ...]: The item to be received
        """
        return (str(self._data.loc[index, ColumnNames.SOURCE.value]),)

    @property
    def data(self) -> DataFrame:
        """
        Property with access to preprocessed DataFrame.

        Returns:
            pandas.DataFrame: Preprocessed DataFrame
        """
        return self._data


class LLMPipeline(AbstractLLMPipeline):
    """
    A class that initializes a model, analyzes its properties and infers it.
    """

    _model: torch.nn.Module

    def __init__(
        self, model_name: str, dataset: TaskDataset, max_length: int, batch_size: int, device: str
    ) -> None:
        """
        Initialize an instance of LLMPipeline.

        Args:
            model_name (str): The name of the pre-trained model
            dataset (TaskDataset): The dataset used
            max_length (int): The maximum length of generated sequence
            batch_size (int): The size of the batch inside DataLoader
            device (str): The device for inference
        """
        super().__init__(model_name, dataset, max_length, batch_size, device)
        self._model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self._model.to(self._device)
        self._tokenizer = AutoTokenizer.from_pretrained(model_name, model_max_length=max_length)

    def analyze_model(self) -> dict:
        """
        Analyze model computing properties.

        Returns:
            dict: Properties of a model
        """
        max_position_embeddings = self._model.config.max_position_embeddings
        input_ids = torch.ones((1, max_position_embeddings), dtype=torch.long)
        input_data = {"input_ids": input_ids, "attention_mask": input_ids}
        model_summary = summary(self._model, input_data=input_data, verbose=False)

        return {
            "input_shape": {"input_ids": list(input_ids.size()),
                            "attention_mask": list(input_ids.size())},
            "embedding_size": self._model.config.max_position_embeddings,
            "output_shape": model_summary.summary_list[-1].output_size,
            "num_trainable_params": model_summary.trainable_params,
            "vocab_size": self._model.config.vocab_size,
            "size": model_summary.total_param_bytes,
            "max_context_length": self._model.config.max_length
        }

    @report_time
    def infer_sample(self, sample: tuple[str, ...]) -> str | None:
        """
        Infer model on a single sample.

        Args:
            sample (tuple[str, ...]): The given sample for inference with model

        Returns:
            str | None: A prediction
        """
        return self._infer_batch([sample])[0]

    @report_time
    def infer_dataset(self) -> pd.DataFrame:
        """
        Infer model on a whole dataset.

        Returns:
            pd.DataFrame: Data with predictions
        """
        data_loader = DataLoader(batch_size=self._batch_size,
                                 dataset=self._dataset,
                                 collate_fn=lambda x: x)

        res = pd.DataFrame(self._dataset.data)
        predictions = []
        for batch in data_loader:
            sample_predictions = self._infer_batch(batch)
            predictions.extend(sample_predictions)

        res[ColumnNames.PREDICTION.value] = predictions

        return res

    @torch.no_grad()
    def _infer_batch(self, sample_batch: Sequence[tuple[str, ...]]) -> list[str]:
        """
        Infer model on a single batch.

        Args:
            sample_batch (Sequence[tuple[str, ...]]): Batch to infer the model

        Returns:
            list[str]: Model predictions as strings
        """
        inputs = self._tokenizer(list(map(lambda x: x[0], sample_batch)),
                                 return_tensors="pt",
                                 padding=True,
                                 truncation=True).to(self._device)

        outputs = self._model(input_ids=inputs["input_ids"],
                              attention_mask=inputs['attention_mask'])
        return [str(pred.item()) for pred in
                torch.argmax(F.softmax(outputs["logits"], dim=1), dim=1)]


class TaskEvaluator(AbstractTaskEvaluator):
    """
    A class that compares prediction quality using the specified metric.
    """

    def __init__(self, data_path: Path, metrics: Iterable[Metrics]) -> None:
        """
        Initialize an instance of Evaluator.

        Args:
            data_path (pathlib.Path): Path to predictions
            metrics (Iterable[Metrics]): List of metrics to check
        """
        self.data_path = data_path
        self._metrics = metrics

    @report_time
    def run(self) -> dict | None:
        """
        Evaluate the predictions against the references using the specified metric.

        Returns:
            dict | None: A dictionary containing information about the calculated metric
        """
        data_frame = pd.read_csv(self.data_path)

        predictions = data_frame[ColumnNames.PREDICTION.value]
        references = data_frame[ColumnNames.TARGET.value]

        evaluation_res = {metric.value: load(metric.value).compute(
            predictions=predictions, references=references,
            average='micro')[metric.value] for metric in self._metrics}

        return evaluation_res
