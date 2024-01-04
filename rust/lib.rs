use crate::pipeline_template_generator::PipelineTemplateGenerator;
mod execution_result;
mod pipeline_template_generator;
use env_logger;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::fmt;
use std::path::PathBuf;

#[derive(Debug)]
struct PlannerError {
    message: String,
}

impl PlannerError {
    fn new(message: &str) -> Self {
        PlannerError {
            message: message.to_string(),
        }
    }
}

impl fmt::Display for PlannerError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "PlannerError: {}", self.message)
    }
}

impl std::error::Error for PlannerError {}

impl From<PlannerError> for PyErr {
    fn from(error: PlannerError) -> PyErr {
        PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(error.to_string())
    }
}

#[pyfunction]
fn create_pipeline_templates(
    model_name: &str,
    tag: &str,
    mut num_nodes: Vec<u32>,
    oobleck_base_dir: Option<PathBuf>,
) -> PyResult<PyObject> {
    num_nodes.sort();

    let mut generator = PipelineTemplateGenerator::new(model_name, tag, oobleck_base_dir);
    generator.divide_and_conquer(num_nodes[num_nodes.len() - 1])?;

    Python::with_gil(|py| {
        let results = PyDict::new(py);

        let module = PyModule::import(py, "oobleck_colossalai.pipeline_template")?;
        let class = module.getattr("PipelineTemplate")?;

        for num_node in num_nodes {
            let template = generator.get_pipeline_template(num_node).unwrap();
            let py_template = class.call1((
                template.latency(),
                template.mem_required(),
                template.get_modules_per_stage(&generator.layer_execution_results),
            ))?;
            results.set_item(num_node, py_template.to_object(py))?;
        }

        Ok(results.to_object(py))
    })
}

#[pymodule]
fn planner(_py: Python, m: &PyModule) -> PyResult<()> {
    let _ = env_logger::try_init();
    m.add_function(wrap_pyfunction!(create_pipeline_templates, m)?)?;
    Ok(())
}
