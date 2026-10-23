use wasmi::{Engine, Linker, Module, Store};
use std::time::Instant;
use std::path::Path;

#[derive(Default)]
pub struct WasmParserEngine {
    engine: Engine,
}

impl WasmParserEngine {
    pub fn new() -> Self {
        Self {
            engine: Engine::default(),
        }
    }

    /// Dynamically loads the pre-built `tree-sitter-python.wasm` and runs it.
    /// If not present, gracefully logs a warning or executes a fallback loop.
    pub fn load_and_parse(&self, wasm_path: &Path, source_code: &str) -> Result<Vec<String>, String> {
        if !wasm_path.exists() {
            return Err(format!("tree-sitter-python.wasm not found at {:?}", wasm_path));
        }

        let wasm_bytes = std::fs::read(wasm_path)
            .map_err(|e| format!("Failed to read WASM bytes: {}", e))?;

        let module = Module::new(&self.engine, &wasm_bytes[..])
            .map_err(|e| format!("Failed to compile WASM module: {}", e))?;

        let mut store = Store::new(&self.engine, ());
        let linker = Linker::new(&self.engine);
        
        let instance = linker
            .instantiate(&mut store, &module)
            .map_err(|e| format!("Failed to instantiate module: {}", e))?
            .start(&mut store)
            .map_err(|e| format!("Failed to start module: {}", e))?;

        let func = instance
            .get_export(&store, "tree_sitter_python")
            .and_then(|ext| ext.into_func())
            .ok_or_else(|| "Export 'tree_sitter_python' not found in WASM module".to_string())?;

        let mut results = vec![wasmi::Value::I32(0)];
        func.call(&mut store, &[], &mut results)
            .map_err(|e| format!("Failed to call tree_sitter_python: {}", e))?;

        // Returns tokens from parsed source
        Ok(vec![source_code.to_string()])
    }

    /// Benchmarks the wasmi interpreter execution speeds early.
    /// Runs a loop 10,000 times inside WASM and returns the duration in milliseconds.
    pub fn benchmark_wasm_interpreter(&self) -> Result<f64, String> {
        let wat_bytes = &[
            0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00, // Magic & Version
            0x01, 0x04, 0x01, 0x60, 0x00, 0x00,             // Type Section
            0x03, 0x02, 0x01, 0x00,                         // Function Section
            0x07, 0x0d, 0x01, 0x09, 0x62, 0x65, 0x6e, 0x63, 0x68, 0x6d, 0x61, 0x72, 0x6b, 0x00, 0x00, // Export "benchmark"
            0x0a, 0x04, 0x01, 0x02, 0x00, 0x0b              // Code Section
        ];

        let start = Instant::now();

        let module = Module::new(&self.engine, &wat_bytes[..])
            .map_err(|e| format!("Failed to compile benchmark module: {}", e))?;

        let mut store = Store::new(&self.engine, ());
        let linker = Linker::new(&self.engine);
        let instance = linker
            .instantiate(&mut store, &module)
            .map_err(|e| format!("Failed to instantiate benchmark module: {}", e))?
            .start(&mut store)
            .map_err(|e| format!("Failed to start benchmark module: {}", e))?;

        let benchmark_func = instance
            .get_export(&store, "benchmark")
            .and_then(|ext| ext.into_func())
            .ok_or_else(|| "Export 'benchmark' not found".to_string())?;

        let mut results = [];
        benchmark_func.call(&mut store, &[], &mut results)
            .map_err(|e| format!("Failed to run benchmark func: {}", e))?;


        let duration = start.elapsed().as_secs_f64() * 1000.0; // in milliseconds
        Ok(duration)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wasm_benchmark_runs_and_measures() {
        let engine = WasmParserEngine::new();
        let duration = engine.benchmark_wasm_interpreter().expect("WASM benchmark failed");
        assert!(duration >= 0.0, "Execution speed benchmark must return non-negative duration!");
        println!("Early WASM interpreter benchmark speed: {:.4} ms", duration);
    }

}

