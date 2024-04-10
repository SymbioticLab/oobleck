import multiprocessing
import os
import runpy
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext, SpawnProcess
from pathlib import Path
from typing import cast

import grpc
import simple_parsing
import torch
from google.protobuf.empty_pb2 import Empty
from loguru import logger

from oobleck.elastic.master_service_pb2 import CodeInfo, DistInfo, PortInfo
from oobleck.elastic.master_service_pb2_grpc import OobleckMasterStub
from oobleck.elastic.run import HostInfo
from oobleck.engine.configuration_engine import ConfigurationEngine


@dataclass
class OobleckAgentArguments:
    master_ip: str
    master_port: int
    agent_index: int
    tag: str
    base_dir: Path


@contextmanager
def temporary_argv(new_argv: list[str]):
    old_argv = sys.argv[:]
    sys.argv = new_argv
    try:
        yield
    finally:
        sys.argv = old_argv


@dataclass
class Worker:
    pipe: Connection
    process: SpawnProcess

    @staticmethod
    def worker_main(
        pipe: Connection,
        agent_index: int,
        gpu_index: int,
        tag: str,
        base_dir: Path,
        script_path: Path,
        script_args: list[str],
    ):
        """
        Worker process main function.

        It creates ConfigurationEngine that will internally be used in
        ExecutionEngine, and execute the given code.
        """
        assert (
            torch.cuda.device_count() == 1
        ), "CUDA_VISIBLE_DEVICES must be set to a single GPU."

        assert ConfigurationEngine._instance is None, (
            "ConfigurationEngine must not be initialized before "
            "worker_main() is called."
        )

        logger.debug(
            f"Worker process started: (agent_index: {agent_index}, "
            f"gpu_index: {os.environ['CUDA_VISIBLE_DEVICES']})"
        )

        ConfigurationEngine.create(pipe, agent_index, gpu_index, tag, base_dir)

        argv = [script_path] + script_args
        with temporary_argv(argv):
            runpy.run_path(script_path.as_posix(), run_name="__main__")


class Agent:
    """Oobleck Agent class.

    For each node, there is one agent process that manages
    worker processes in the node.
    """

    def __init__(
        self,
        agent_index: int,
        job_tag: str,
        base_dir: Path,
        stub: OobleckMasterStub,
    ):
        self.agent_index = agent_index
        self.tag = job_tag
        self.base_dir = base_dir
        self.stub = stub

        # Get distributed information and code from the master
        dist_info: DistInfo = stub.GetDistInfo(Empty())
        self.dist_info = list(
            HostInfo(host.ip, host.devices, host.port) for host in dist_info.hosts
        )
        training_args: CodeInfo = stub.GetCode(Empty())
        self.script: Path = Path(training_args.path)
        self.script_args: list[str] = [arg for arg in training_args.args]
        self.workers: list[Worker] = []

    def notify_reconfiguration_to_workers(self, dist_info: list[HostInfo]):
        logger.warning(
            f"Reconfiguration request received from master: {dist_info}. Sending to workers"
        )
        for worker in self.workers:
            worker.pipe.send("reconfigure")
            worker.pipe.send(dist_info)

        self.forward_master_port()

    def watch_reconfiguration_notification(self):
        for dist_info in stub.WatchReconfigurationNotification(Empty()):
            dist_info = cast(DistInfo, dist_info)
            dist_info = [
                HostInfo(host.ip, host.devices, host.port) for host in dist_info.hosts
            ]
            self.notify_reconfiguration_to_workers(dist_info)

    def run_profiler(self):
        raise NotImplementedError()

    def launch_workers(self):
        """Launch worker processes."""
        ctx: SpawnContext = multiprocessing.get_context("spawn")

        gpu_indices: list[int] = list(
            int(dev) for dev in self.dist_info[self.agent_index].devices.split(",")
        )

        tensor_parallel_size = len(gpu_indices)
        ranks = range(
            self.agent_index * tensor_parallel_size,
            (self.agent_index + 1) * tensor_parallel_size,
        )

        os.environ["TORCH_NCCL_USE_COMM_NONBLOCKING"] = "1"
        os.environ["TORCH_NCCL_ASYNC_ERROR_HANDLING"] = "0"
        for gpu_index, rank in zip(gpu_indices, ranks):
            logger.info(f"Launching worker {rank} (GPU: {gpu_index})...")

            pipe, child_pipe = ctx.Pipe()

            os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
            process: SpawnProcess = ctx.Process(
                target=Worker.worker_main,
                args=(
                    child_pipe,
                    self.agent_index,
                    gpu_index % tensor_parallel_size,
                    self.tag,
                    self.base_dir,
                    self.script,
                    self.script_args,
                ),
            )
            process.start()
            self.workers.append(Worker(pipe, process))
            pipe.send(self.dist_info)
        del os.environ["CUDA_VISIBLE_DEVICES"]

        self.forward_master_port()

    def forward_master_port(self):
        """
        Forward master port after receiving it from the master
        to all worker processes.

        If this is the first agent, it should send the rank 0 port
        from its first worker to master too.
        """
        # If this is the first agent, it should forward the master rank port
        if self.agent_index == 0:
            logger.debug("Waiting for rank 0 port...")
            port: int = self.workers[0].pipe.recv()
            logger.debug(f"Received rank 0 port: {port}. Sending it to master.")
            self.stub.SetMasterRankPort(PortInfo(port=port))

        port: int = 0
        while port == 0:
            time.sleep(0.1)
            port = self.stub.GetMasterRankPort(Empty()).port

        for worker in self.workers:
            worker.pipe.send(port)

    def watch_worker_exit(self):
        """Watch worker exit and restart it.
        TODO: It must detect ANY worker exit, not just the first one."""
        for worker in self.workers:
            worker.process.join()
            if worker.process.exitcode != 0:
                logger.warning(
                    f"Worker {worker.process.pid} exited with code {worker.process.exitcode}."
                )
        logger.info("All workers exited.")


if __name__ == "__main__":
    args: OobleckAgentArguments = simple_parsing.parse(
        OobleckAgentArguments, dest="agent"
    )

    # Connect to the master
    channel = grpc.insecure_channel(f"{args.master_ip}:{args.master_port}")
    stub = OobleckMasterStub(channel)

    agent = Agent(args.agent_index, args.tag, args.base_dir, stub)
    agent.launch_workers()

    def watch_reconfiguration_noti_func():
        while True:
            agent.watch_reconfiguration_notification()

    thread = threading.Thread(target=watch_reconfiguration_noti_func, daemon=True)
    thread.start()

    agent.watch_worker_exit()
    logger.info(f"Agent {args.agent_index} exited.")
