from __future__ import annotations

import gc
import logging
import math
import multiprocessing as mp
import random
import time
import traceback
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from unittest.mock import patch

import deepspeed.comm as dist
import pytest
import torch
import torch.distributed
from pytest_mock import MockerFixture
from transformers.training_args import TrainingArguments

from oobleck.csrc.planning.pipeline_template import (
    LayerExecutionResult,
    LayerExecutionResults,
    PipelineTemplate,
    StageExecutionResult,
)
from oobleck.execution.dataloader import LoaderType, OobleckDataLoader
from oobleck.execution.dataset import OobleckDataset
from oobleck.execution.pipeline import OobleckPipeline
from oobleck.module.model import OobleckModel

TRAIN_BATCH_SIZE = 1
EVAL_BATCH_SIZE = 2
GRADIENT_ACCUMULATION_STEP = 4

logging.basicConfig(level=logging.INFO)


@dataclass
class Model:
    model_name: str
    dataset_path: str
    dataset_name: str | None = None


models_to_test: dict[str, Model] = {
    "gpt2": Model("gpt2", "wikitext", "wikitext-2-raw-v1"),
    "microsoft/resnet-50": Model("microsoft/resnet-50", "Maysee/tiny-imagenet"),
}

# Add model arguments here, if it is needed.
model_args: dict[str, dict[str, int] | None] = {
    "gpt2": {
        "num_hidden_layers": 32,
        "n_positions": 1024,
        "n_embd": 1024,
        "n_head": 16,
    },
}


@pytest.fixture(scope="session", params=list(models_to_test.keys()))
def model_name_fixture(request: pytest.FixtureRequest) -> str:
    return request.param


class OobleckStaticClassFactory:
    """
    Oobleck Class Factory that create classes for testing.
    "Static" here means that it is not relevant to Oobleck dynamic reconfiguration
    and fixed once a class object is created.
    """

    def __init__(self, model_name: str, test_directory: Path):
        self._model_data: Model = models_to_test[model_name]
        self._training_args = TrainingArguments(
            output_dir=test_directory,
            per_device_train_batch_size=TRAIN_BATCH_SIZE,
            per_device_eval_batch_size=EVAL_BATCH_SIZE,
            gradient_accumulation_steps=GRADIENT_ACCUMULATION_STEP,
        )

        self._dataset: OobleckDataset | None = None
        self._model: OobleckModel | None = None
        self._dataloader: OobleckDataLoader | None = None
        self._profile: LayerExecutionResults | None = None
        self._pipeline_templates: dict[int, PipelineTemplate] = {}

    def get_dataset(self) -> OobleckDataset:
        if not self._dataset:
            self._dataset = OobleckDataset(
                self._model_data.model_name,
                self._model_data.dataset_path,
                self._model_data.dataset_name,
            )
        return self._dataset

    def get_model(self) -> OobleckModel:
        self.get_dataset()

        if not self._model:
            self._model = OobleckModel(
                self._model_data.model_name,
                self._dataset.sample,
                self._training_args,
                "test",
                model_args.get(self._model_data.model_name, None),
            )

        return self._model

    def get_dummy_profile(self) -> LayerExecutionResults:
        self.get_model()

        if not self._profile:
            num_layers = len(self._model.model)

            results: list[LayerExecutionResult] = []
            for index in range(num_layers):
                results.append(
                    LayerExecutionResult(
                        layer_index=index,
                        forward=random.random(),
                        backward=random.random() * 3,
                        allreduce_in_node={i + 1: random.random() for i in range(8)},
                        allreduce_across_nodes={
                            i + 1: random.random() * 4 for i in range(64)
                        },
                        mem_required=(1024, 1024),
                    )
                )

            self._profile = LayerExecutionResults(results)

        return self._profile

    def get_dummy_pipeline_template(
        self,
        num_stages: int,
        num_nodes: int,
        num_gpus_per_node: int = 1,
    ) -> PipelineTemplate:
        self.get_dummy_profile()

        def slice_layers(lst: list[Any], num_chunks: int) -> list[tuple[int, int]]:
            if num_chunks > len(lst):
                raise ValueError(
                    f"Cannot slice {len(list)} layers into {num_chunks} chunks."
                )

            length_chunk = math.ceil(len(lst) / num_chunks)
            slicing_points: list[tuple[int, int]] = []
            for i in range(0, len(lst), length_chunk):
                end = i + length_chunk if i + length_chunk < len(lst) else len(lst)
                slicing_points.append((i, end))
            return slicing_points

        assert (
            num_nodes * num_gpus_per_node
        ) % num_stages == 0, "Stages in dummy pipeline template must have equal size."

        key = (num_stages, num_nodes, num_gpus_per_node)
        if key not in self._pipeline_templates:
            layer_indices = slice_layers(self._profile.get(), num_stages)

            num_gpus_per_stage = (num_nodes * num_gpus_per_node) // num_stages
            stages = [
                StageExecutionResult(self._profile, indices, num_gpus_per_stage)
                for indices in layer_indices
            ]

            self._pipeline_templates[key] = PipelineTemplate(
                stages,
                0.1,
                self._profile.size,
                num_nodes,
                num_gpus_per_node,
            )

        return self._pipeline_templates[key]


class OobleckDynamicClassFactory:
    """
    Oobleck Class Factory that create classes for testing.
    "Dynamic" here means that the internal states are changed during training.
    Thus the class object should be created every time a new state is needed.
    """

    def __init__(
        self, static_factory: OobleckStaticClassFactory, my_rank: int, ranks: list[int]
    ):
        assert dist.is_initialized()
        assert torch.distributed.is_initialized()

        self._static_factory = static_factory
        self._my_rank = my_rank
        self._ranks = ranks

    def get_dataloader(
        self,
        pipeline_index: int,
        num_microbatches: list[int],
        num_iterations: int = 0,
    ) -> OobleckDataLoader:
        dataset = self._static_factory.get_dataset()
        training_args = self._static_factory._training_args

        return OobleckDataLoader(
            args=training_args,
            datasets=dataset,
            dataloader_type=LoaderType.Training,
            pipeline_index=pipeline_index,
            num_microbatches=num_microbatches,
            num_iterations_done=num_iterations,
            epoch=0,
            shuffle=False,
        )

    def get_dummy_pipeline(
        self,
        num_stages: int,
        num_gpus_per_node: int = 1,
    ) -> OobleckPipeline:
        num_nodes = 1
        model = self._static_factory.get_model()
        # TODO: make this more flexible
        template = self._static_factory.get_dummy_pipeline_template(
            num_stages=num_stages,
            num_nodes=num_nodes,
            num_gpus_per_node=num_gpus_per_node,
        )
        training_args = self._static_factory._training_args
        dataloader = self.get_dataloader(0, [training_args.gradient_accumulation_steps])

        pipeline = OobleckPipeline(
            pipeline_id=0,
            pipeline_template=template,
            ranks=self._ranks,
            dataloader=dataloader,
            step=0,
            training_args=training_args,
        )

        pipeline.initialize_distributed_fsdp(model)
        pipeline.initialize_distributed_pipeline()

        return pipeline


@pytest.fixture(scope="session", autouse=True)
def factory(
    model_name_fixture: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> OobleckStaticClassFactory:
    directory = tmp_path_factory.mktemp(
        f"single_process_{model_name_fixture.replace('/', '-')}"
    )
    return OobleckStaticClassFactory(model_name_fixture, directory)


class OobleckSingleProcessTestCase:
    """
    A base class for Oobleck test cases that run in a single process.
    Test cases for functionalities of static classes will inherit this class.
    """

    @pytest.fixture(scope="function", autouse=False)
    def distributed(self, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch):
        assert not dist.is_initialized() and not torch.distributed.is_initialized()

        # envs required by deepspeed.comm
        monkeypatch.setenv("RANK", "0")
        monkeypatch.setenv("WORLD_SIZE", "1")

        # Initialize a single process torch.distributed group.
        store = torch.distributed.HashStore()
        torch.distributed.init_process_group(
            backend="nccl", store=store, rank=0, world_size=1
        )
        dist.init_distributed(dist_backend="nccl", dist_init_required=False)
        assert torch.distributed.is_initialized()
        assert dist.is_initialized()

        yield

        dist.destroy_process_group()
        dist.cdb = None
        assert not torch.distributed.is_initialized()
        assert not dist.is_initialized()


class OobleckTestProcess:
    def __init__(
        self,
        pipe: Connection,
        rank: int,
        model_name: str,
        directory: Path,
    ):
        logging.info(f"Launching rank{rank} with model {model_name}")
        self._pipe = pipe
        self._rank = rank
        self._directory = directory

        # Very careful initialization dependency due to too many third-party libraries.
        # As we use torch.distributed.FileStore for distributed initialization, it doesn't require
        # os envs (MASTER_ADDR, MASTER_PORT), while deepspeed and HuggingFace by default use them.
        # Thus, initialize StaticClassFactory (which relies on HF) first without the envs.
        # Then, initialize distributed and deepspeed.
        # After that, create dynamic class factory since it requires distributed configuration.
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", str(rank))
        monkeypatch.delenv("RANK", raising=False)
        monkeypatch.delenv("WORLD_SIZE", raising=False)

        patcher = patch("torch.cuda.device_count", return_value=1)
        patcher.start()

        self.factory = OobleckStaticClassFactory(model_name, directory)

        self.run()

    def run(self):
        test: Callable
        args: tuple

        monkeypatch = pytest.MonkeyPatch()

        while True:
            try:
                test, world_size, args = self._pipe.recv()
                if test is None:
                    break

                monkeypatch.setenv("RANK", str(self._rank))
                monkeypatch.setenv("WORLD_SIZE", str(world_size))
                torch.cuda.set_device(0)

                store = torch.distributed.FileStore(
                    str(self._directory / f"{world_size}_{test.__name__}_store"),
                    world_size,
                )

                torch.distributed.init_process_group(
                    backend="nccl",
                    store=store,
                    rank=self._rank,
                    world_size=world_size,
                )
                dist.init_distributed(dist_backend="nccl", dist_init_required=False)

                dfactory = OobleckDynamicClassFactory(
                    self.factory, self._rank, list(range(world_size))
                )
                logging.info(
                    f"Running test in rank{self._rank} / {world_size}: {test.__name__}"
                )
                result = test(self.factory, dfactory, *args)
                logging.info("Test done.")

                self._pipe.send(
                    {
                        "success": (result if result is not None else ""),
                    }
                )
            except Exception as e:
                logging.error(f"Rank {self._rank} failed with exception: {e}")
                self._pipe.send({"error": str(e) + "\n" + traceback.format_exc()})
            finally:
                torch.distributed.barrier()
                torch.distributed.destroy_process_group()
                dist.cdb = None
                store = self._directory / f"{world_size}_{test.__name__}_store"
                store.unlink(missing_ok=True)
                while store.exists():
                    logging.info("Waiting for store to be deleted...")
                    time.sleep(1)
                monkeypatch.undo()

                # Release the GPU memory and check
                obj: torch.Tensor
                for obj in gc.get_objects():
                    if torch.is_tensor(obj) and obj.is_cuda:
                        if obj.grad is not None:
                            obj.grad.data = torch.empty(0)
                        obj.data = torch.empty(0)
                gc.collect()
                torch.cuda.empty_cache()
                if torch.cuda.memory_allocated() > (1000 * 2**20):
                    logging.fatal("Failed to reclaim GPU memory. Abort testing.")
                    self._pipe.close()
                    return

        logging.info(f"Rank {self._rank} in world size {world_size} finished.")


processes: dict[str, list[tuple[mp.Process, Connection]]] = defaultdict(list)


@pytest.mark.skipif(torch.cuda.device_count() < 4, reason="requires 4 GPUs")
class OobleckMultiProcessTestCase:
    """
    A base class for Oobleck test cases that run in multiple processes in parallel.
    Test cases for functionalities of dynamic classes will inherit this class.
    """

    def get_processes(
        self, num_processes: int, model_name: str, directory: Path
    ) -> list[tuple[mp.Process, Connection]]:
        global processes
        if model_name in processes:
            return processes[model_name][:num_processes]

        # always initialize 4 processes
        ctx = mp.get_context("spawn")
        for rank in range(4):
            pipe = ctx.Pipe()
            p = ctx.Process(
                target=OobleckTestProcess,
                args=(
                    pipe[1],
                    rank,
                    model_name,
                    directory,
                ),
                daemon=True,
            )
            p.start()
            processes[model_name].append((p, pipe[0]))

        return processes[model_name][:num_processes]

    model_name: str
    tmp_directory: Path

    @pytest.fixture(scope="session", autouse=True)
    def directory(
        self, tmp_path_factory: pytest.TempPathFactory, model_name_fixture: str
    ):
        return tmp_path_factory.mktemp(
            f"multi_process_{model_name_fixture.replace('/', '-')}"
        )

    @classmethod
    @pytest.fixture(scope="class", autouse=True)
    def setup_class(
        cls, model_name_fixture: str, directory: Path, request: pytest.FixtureRequest
    ):
        request.cls.model_name = model_name_fixture
        request.cls.tmp_directory = directory

    def run_in_parallel(
        self, num_processes: int, func: Callable, *args
    ) -> list[str | None]:
        procs = self.get_processes(num_processes, self.model_name, self.tmp_directory)
        for _, pipe in procs:
            pipe.send((func, num_processes, args))

        results: list[Any] = [None] * len(procs)
        try:
            for index, (_, pipe) in enumerate(procs):
                # if not pipe.poll(timeout=60):
                #     raise TimeoutError()

                try:
                    result = pipe.recv()
                except Exception:
                    for proc, _ in procs:
                        proc.kill()
                    pytest.exit("Aborted due to GPU reclaim failure.", returncode=1)

                if "error" in result:
                    # If any process get an error,
                    # immediately abort the test.
                    raise RuntimeError(result["error"])
                else:
                    results[index] = result["success"]
        except Exception as e:
            logging.error(f"Failed with exception: {e}")
            raise e

        return results
