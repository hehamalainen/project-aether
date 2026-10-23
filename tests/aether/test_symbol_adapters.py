from packages.aether.services.symbol_adapters import extract_symbol_spans, find_symbol_at_line


def test_js_symbol_extraction_finds_functions():
    source = (
        "function alpha() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function beta() {\n"
        "  return 2;\n"
        "}\n"
    )
    spans = extract_symbol_spans("main.ts", source)
    ids = {span.symbol_id for span in spans}
    assert any(symbol.startswith("FUNC:alpha") for symbol in ids)
    assert any(symbol.startswith("FUNC:beta") for symbol in ids)


def test_find_symbol_at_line_returns_expected_symbol():
    source = (
        "function alpha() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function beta() {\n"
        "  return 2;\n"
        "}\n"
    )
    spans = extract_symbol_spans("main.ts", source)
    assert find_symbol_at_line(spans, 1).startswith("FUNC:alpha")
    assert find_symbol_at_line(spans, 5).startswith("FUNC:beta")


def test_go_symbol_extraction_finds_functions_and_types():
    source = (
        "type Worker struct {\n"
        "  Name string\n"
        "}\n"
        "\n"
        "func alpha() int {\n"
        "  return 1\n"
        "}\n"
    )
    spans = extract_symbol_spans("main.go", source)
    ids = {span.symbol_id for span in spans}
    assert any(symbol.startswith("TYPE:Worker") for symbol in ids)
    assert any(symbol.startswith("FUNC:alpha") for symbol in ids)


def test_java_symbol_extraction_finds_class_and_method():
    source = (
        "public class Demo {\n"
        "  public int alpha() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
    )
    spans = extract_symbol_spans("Demo.java", source)
    ids = {span.symbol_id for span in spans}
    assert any(symbol.startswith("CLASS:Demo") for symbol in ids)
    assert any(symbol.startswith("METHOD:alpha") for symbol in ids)


def test_rust_symbol_extraction_finds_type_and_function():
    source = (
        "pub struct Worker {\n"
        "  id: i32,\n"
        "}\n"
        "\n"
        "pub fn alpha() -> i32 {\n"
        "  1\n"
        "}\n"
    )
    spans = extract_symbol_spans("lib.rs", source)
    ids = {span.symbol_id for span in spans}
    assert any(symbol.startswith("TYPE:Worker") for symbol in ids)
    assert any(symbol.startswith("FUNC:alpha") for symbol in ids)
