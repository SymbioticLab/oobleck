import pytest
from oobleck_colossalai.pipeline_template import PipelineTemplate
from oobleck import planner
from pathlib import Path
import csv

tag = "gpt2-test"


@pytest.fixture()
def base_dir(tmp_path: Path) -> Path:
    path = tmp_path / "profiles" / f"{tag}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        fieldnames = [
            "layer_index",
            "layer_name",
            "forward",
            "backward",
            "mem_required",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        writer.writeheader()
        for i in range(6):
            writer.writerow(
                {
                    "layer_index": i,
                    "layer_name": f"layer_{i}",
                    "forward": i + 1,
                    "backward": 1 + 1,
                    "mem_required": i + 1,
                }
            )

    return tmp_path


def test_error_for_too_large_num_nodes(base_dir: Path):
    with pytest.raises(RuntimeError):
        planner.create_pipeline_templates(
            tag="gpt2-test", num_nodes=[8], oobleck_base_dir=base_dir
        )


def test_create_pipeline_templates(base_dir: Path):
    templates: dict[int, PipelineTemplate] = planner.create_pipeline_templates(
        tag="gpt2-test", num_nodes=[1, 2, 3, 4], oobleck_base_dir=base_dir
    )

    expected_layers = [f"layer_{i}" for i in range(6)]

    assert sorted(list(templates.keys())) == [1, 2, 3, 4]
    for _, template in templates.items():
        assert isinstance(template, PipelineTemplate)
        covered_layers = []
        for stage in template.modules_per_stage:
            for layer in stage:
                covered_layers.append(layer)

        assert expected_layers == covered_layers
