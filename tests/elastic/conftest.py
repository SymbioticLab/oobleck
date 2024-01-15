import multiprocessing
from concurrent import futures
from pathlib import Path

import grpc
import pytest

from oobleck.elastic import master_service_pb2_grpc
from oobleck.elastic.run import HostInfo, LaunchArgs, ScriptArgs, MasterService

fake_host_info = [
    HostInfo("127.0.0.1", 2, 1234),
    HostInfo("127.0.0.2", 2, 1234),
    HostInfo("127.0.0.3", 2, 1234),
]


@pytest.fixture()
def server(tmp_path: Path) -> tuple[LaunchArgs, ScriptArgs, MasterService, int]:
    fake_launch_args = LaunchArgs(
        hostfile=Path(tmp_path / "hostfile"),
        output_dir=tmp_path,
    )

    fake_launch_args.hostfile.write_text(
        "\n".join(
            list(
                f"{host.ip} slots={host.slots} port={host.port}"
                for host in fake_host_info
            )
        )
    )

    fake_script_args = ScriptArgs(
        training_script=Path(tmp_path / "testscript.py"),
        training_script_args=["--foo", "bar", "--baz", "qux"],
    )

    fake_script_args.training_script.write_text(
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--foo')\n"
        "parser.add_argument('--baz')\n"
        "args = parser.parse_args()\n"
        "print(f'Hello, {args.foo}, {args.baz}')\n"
    )

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    service = MasterService(
        fake_script_args,
        fake_host_info,
        multiprocessing.get_context("spawn").Condition(),
    )
    master_service_pb2_grpc.add_OobleckMasterServicer_to_server(service, server)
    port = server.add_insecure_port(f"0.0.0.0:0")
    server.start()

    yield fake_launch_args, fake_script_args, service, port
    server.stop(grace=None)
