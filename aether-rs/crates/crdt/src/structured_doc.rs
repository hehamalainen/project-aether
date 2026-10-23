use crate::rga::RGADocument;
use std::collections::HashMap;
use tree_sitter::{Node, Parser};

pub struct StructuredDocument {
    pub replica_id: String,
    pub nodes: HashMap<String, RGADocument>,
    pub node_order: Vec<String>,
}

impl StructuredDocument {
    pub fn new(replica_id: String) -> Self {
        Self {
            replica_id,
            nodes: HashMap::new(),
            node_order: Vec::new(),
        }
    }

    pub fn set_content(&mut self, source_code: &str) {
        let mut parser = Parser::new();
        if parser
            .set_language(&tree_sitter_python::LANGUAGE.into())
            .is_err()
        {
            self.set_raw_node(source_code);
            return;
        }

        let tree = parser.parse(source_code, None);
        if let Some(tree) = tree {
            if tree.root_node().has_error() {
                self.set_raw_node(source_code);
            } else {
                self.decompose_tree(tree.root_node(), source_code);
            }
        } else {
            self.set_raw_node(source_code);
        }
    }

    fn set_raw_node(&mut self, content: &str) {
        let node_id = "__raw__".to_string();
        let mut doc = RGADocument::new(self.replica_id.clone());
        doc.replace_text(content);
        self.nodes.insert(node_id.clone(), doc);
        self.node_order = vec![node_id];
    }

    fn add_node(&mut self, base_id: &str, content: &str) {
        self.add_node_and_return_id(base_id, content);
    }

    fn add_node_and_return_id(&mut self, base_id: &str, content: &str) -> String {
        let mut node_id = base_id.to_string();
        let mut suffix = 2;
        while self.nodes.contains_key(&node_id) {
            node_id = format!("{}#{}", base_id, suffix);
            suffix += 1;
        }

        let mut doc = RGADocument::new(self.replica_id.clone());
        doc.replace_text(content);
        self.nodes.insert(node_id.clone(), doc);
        if !self.node_order.contains(&node_id) {
            self.node_order.push(node_id.clone());
        }
        node_id
    }

    fn append_to_node(&mut self, node_id: &str, content: &str) {
        if let Some(doc) = self.nodes.get_mut(node_id) {
            let current = doc.get_text();
            doc.replace_text(&format!("{}{}", current, content));
        } else {
            self.add_node(node_id, content);
        }
    }

    fn decompose_tree(&mut self, root: Node, source_code: &str) {
        self.nodes.clear();
        self.node_order.clear();

        let mut cursor = root.walk();
        let mut has_next = cursor.goto_first_child();

        let mut last_byte = 0;
        let mut active_group_id: Option<String> = None;

        while has_next {
            let node = cursor.node();
            let kind = node.kind();

            let mut prefix = "";
            // Capture any leading whitespace/comments before this node
            if node.start_byte() > last_byte {
                let gap = &source_code[last_byte..node.start_byte()];
                if last_byte == 0 {
                    // Very first gap goes to __header__
                    self.add_node("__header__", gap);
                    active_group_id = Some("__header__".to_string());
                } else {
                    prefix = gap;
                }
            }

            let text = &source_code[node.start_byte()..node.end_byte()];
            let combined_text = format!("{}{}", prefix, text);

            let mut target_base = "__globals__".to_string();

            match kind {
                "import_statement" | "import_from_statement" => {
                    target_base = "__imports__".to_string();
                }
                "function_definition" | "decorated_definition" => {
                    let mut name = "anonymous";
                    let mut is_class = false;
                    let mut def_node = node;

                    if kind == "decorated_definition" {
                        if let Some(def) = node.child_by_field_name("definition") {
                            def_node = def;
                        }
                    }

                    if def_node.kind() == "class_definition" {
                        is_class = true;
                        if let Some(name_node) = def_node.child_by_field_name("name") {
                            name = &source_code[name_node.start_byte()..name_node.end_byte()];
                        }
                    } else {
                        if let Some(name_node) = def_node.child_by_field_name("name") {
                            name = &source_code[name_node.start_byte()..name_node.end_byte()];
                        }
                    }

                    if is_class {
                        let class_prefix = if kind == "decorated_definition" {
                            format!(
                                "{}{}",
                                prefix,
                                &source_code[node.start_byte()..def_node.start_byte()]
                            )
                        } else {
                            prefix.to_string()
                        };
                        self.decompose_class(def_node, name, source_code, &class_prefix);
                        active_group_id = None; // Reset group since decompose_class handles its own
                        last_byte = node.end_byte();
                        has_next = cursor.goto_next_sibling();
                        continue;
                    } else {
                        target_base = format!("FUNC:{}", name);
                    }
                }
                "class_definition" => {
                    let mut name = "anonymous";
                    if let Some(name_node) = node.child_by_field_name("name") {
                        name = &source_code[name_node.start_byte()..name_node.end_byte()];
                    }
                    self.decompose_class(node, name, source_code, prefix);
                    active_group_id = None;
                    last_byte = node.end_byte();
                    has_next = cursor.goto_next_sibling();
                    continue;
                }
                _ => {}
            }

            // To preserve chronological exactness, we only group if the target_base
            // matches the currently active group. Otherwise we add a new node.
            if let Some(active) = &active_group_id {
                if active.starts_with(&target_base) {
                    self.append_to_node(active, &combined_text);
                } else {
                    let new_id = self.add_node_and_return_id(&target_base, &combined_text);
                    active_group_id = Some(new_id);
                }
            } else {
                let new_id = self.add_node_and_return_id(&target_base, &combined_text);
                active_group_id = Some(new_id);
            }

            last_byte = node.end_byte();
            has_next = cursor.goto_next_sibling();
        }

        // Footer
        if last_byte < source_code.len() {
            let gap = &source_code[last_byte..source_code.len()];
            self.add_node("__footer__", gap);
        }
    }

    fn decompose_class(
        &mut self,
        class_node: Node,
        class_name: &str,
        source_code: &str,
        prefix: &str,
    ) {
        if let Some(body_node) = class_node.child_by_field_name("body") {
            let class_start = class_node.start_byte();
            let body_start = body_node.start_byte();

            // Add the class header (everything up to the body block, including leading trivia)
            let header_text = format!("{}{}", prefix, &source_code[class_start..body_start]);
            self.add_node(&format!("CLASS:{}::__head__", class_name), &header_text);

            let mut cursor = body_node.walk();
            let mut has_next = cursor.goto_first_child();
            let mut last_byte = body_start;

            while has_next {
                let node = cursor.node();

                let mut inner_prefix = "";
                if node.start_byte() > last_byte {
                    inner_prefix = &source_code[last_byte..node.start_byte()];
                }

                let text = &source_code[node.start_byte()..node.end_byte()];
                let combined_text = format!("{}{}", inner_prefix, text);

                if node.kind() == "function_definition" || node.kind() == "decorated_definition" {
                    let mut func_name = "anonymous";
                    let mut def_node = node;
                    if node.kind() == "decorated_definition" {
                        if let Some(def) = node.child_by_field_name("definition") {
                            def_node = def;
                        }
                    }
                    if let Some(name_node) = def_node.child_by_field_name("name") {
                        func_name = &source_code[name_node.start_byte()..name_node.end_byte()];
                    }
                    self.add_node(
                        &format!("CLASS:{}::FUNC:{}", class_name, func_name),
                        &combined_text,
                    );
                } else {
                    self.append_to_node(&format!("CLASS:{}::__head__", class_name), &combined_text);
                }

                last_byte = node.end_byte();
                has_next = cursor.goto_next_sibling();
            }

            if last_byte < class_node.end_byte() {
                let gap = &source_code[last_byte..class_node.end_byte()];
                self.append_to_node(&format!("CLASS:{}::__head__", class_name), gap);
            }
        } else {
            let text = &source_code[class_node.start_byte()..class_node.end_byte()];
            let combined_text = format!("{}{}", prefix, text);
            self.add_node(&format!("CLASS:{}", class_name), &combined_text);
        }
    }

    pub fn get_full_text(&self) -> String {
        let mut result = String::new();
        for node_id in &self.node_order {
            if let Some(doc) = self.nodes.get(node_id) {
                result.push_str(&doc.get_text());
            }
        }
        result
    }

    pub fn get_node_at_line(&self, line_num: usize) -> Option<String> {
        let mut current_line = 1;
        for node_id in &self.node_order {
            if let Some(doc) = self.nodes.get(node_id) {
                let node_text = doc.get_text();
                let count_newlines = node_text.matches('\n').count();
                let has_newline_at_end = node_text.ends_with('\n');
                let node_lines = count_newlines + if !node_text.is_empty() && !has_newline_at_end { 1 } else { 0 };
                if line_num >= current_line && line_num < current_line + node_lines {
                    return Some(node_id.clone());
                }
                current_line += node_lines;
            }
        }
        None
    }
}

/// Deterministically tokenizes a source file using tree-sitter-python, walking leaf nodes
/// and preserving all exact byte spacing as interleaved whitespace/trivia tokens.
/// If there is an AST parsing error, it falls back to basic line/whitespace tokenization.
pub fn tokenize_source(source_code: &str) -> Vec<String> {
    let mut parser = Parser::new();
    if parser.set_language(&tree_sitter_python::LANGUAGE.into()).is_err() {
        return vec![source_code.to_string()];
    }

    let tree = parser.parse(source_code, None);
    if let Some(tree) = tree {
        if tree.root_node().has_error() {
            // Error fallback: split by whitespace deterministically while preserving whitespaces
            let mut tokens = Vec::new();
            let mut current = String::new();
            for c in source_code.chars() {
                if c.is_whitespace() {
                    if !current.is_empty() {
                        tokens.push(current.clone());
                        current.clear();
                    }
                    tokens.push(c.to_string());
                } else {
                    current.push(c);
                }
            }
            if !current.is_empty() {
                tokens.push(current);
            }
            return tokens;
        }
        
        let mut tokens = Vec::new();
        let mut last_byte = 0;
        
        fn traverse_leaves<'a>(
            node: Node<'a>,
            source_code: &'a str,
            tokens: &mut Vec<String>,
            last_byte: &mut usize,
        ) {
            if node.child_count() == 0 {
                let start = node.start_byte();
                let end = node.end_byte();
                
                // Capture preceding gap (whitespace/trivia)
                if start > *last_byte {
                    let gap = &source_code[*last_byte..start];
                    tokens.push(gap.to_string());
                }
                
                let token_text = &source_code[start..end];
                if !token_text.is_empty() {
                    tokens.push(token_text.to_string());
                }
                
                *last_byte = end;
            } else {
                let mut cursor = node.walk();
                let mut has_next = cursor.goto_first_child();
                while has_next {
                    traverse_leaves(cursor.node(), source_code, tokens, last_byte);
                    has_next = cursor.goto_next_sibling();
                }
            }
        }
        
        traverse_leaves(tree.root_node(), source_code, &mut tokens, &mut last_byte);
        
        // Trailing gap
        if last_byte < source_code.len() {
            let trailing = &source_code[last_byte..];
            tokens.push(trailing.to_string());
        }
        
        tokens
    } else {
        vec![source_code.to_string()]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rga::RGADocument;

    #[test]
    fn test_structured_doc_roundtrip_edge_cases() {
        let code = r#"#!/usr/bin/env python3
# Global comment island

import os
from typing import Optional

@decorator(
    arg=1
)
def simple_func():
    pass

class ComplexClass:
    """Class docstring"""
    
    # Class body comment
    class_attr = 1
    
    @staticmethod
    def nested_func(x):
        # Nested comment
        return x + 1

@class_decorator(
    option=True
)
class DecoratedClass:
    pass

# Trailing global comment
if __name__ == '__main__':
    main()
"#;

        let mut doc = StructuredDocument::new("test_replica".to_string());
        doc.set_content(code);

        let restored = doc.get_full_text();
        assert_eq!(
            restored, code,
            "Roundtrip failed to preserve exact byte structure!"
        );
    }

    #[test]
    fn test_deterministic_tokenization_roundtrip() {
        let code = r#"def add(x, y):
    # Deterministic comments
    return x + y
"#;
        let tokens = tokenize_source(code);
        let joined = tokens.join("");
        assert_eq!(joined, code, "Tree-sitter token join must match original code exactly!");
        
        // Verify key tokens are parsed discretely
        assert!(tokens.contains(&"def".to_string()));
        assert!(tokens.contains(&"add".to_string()));
        assert!(tokens.contains(&"return".to_string()));
    }

    #[test]
    fn test_token_rga_document_roundtrip() {
        let code = r#"class Machine:
    def spin(self):
        return "vroom"
"#;
        let tokens = tokenize_source(code);
        
        let mut rga = RGADocument::new("replica_a".to_string());
        rga.replace_tokens(&tokens);
        
        let restored = rga.get_text();
        assert_eq!(restored, code, "Restored text from token RGA document must match original!");
    }

    #[test]
    fn test_token_rga_merge_concurrency() {
        let code_base = "def test():\n    pass\n";
        let tokens_base = tokenize_source(code_base);

        let mut rga_a = RGADocument::new("replica_a".to_string());
        rga_a.replace_tokens(&tokens_base);

        let mut rga_b = rga_a.clone();
        rga_b.replica_id = "replica_b".to_string();

        // Replica A inserts a token locally
        let op_a = rga_a.local_insert(2, " # replica_a edit".to_string());

        // Replica B inserts a token locally at the same offset
        let op_b = rga_b.local_insert(2, " # replica_b edit".to_string());

        // Concurrent merge
        rga_a.apply_operation(&op_b);
        rga_b.apply_operation(&op_a);

        let text_a = rga_a.get_text();
        let text_b = rga_b.get_text();

        assert_eq!(text_a, text_b, "CRDT RGA concurrent token merge failed to converge!");
        println!("Converged output:\n{}", text_a);
    }

    #[test]
    fn test_token_mapping_stability_under_shifts() {
        let code_v1 = "x = 42\ny = 100\n";
        let code_v2 = "x = 42  # formatted spacing\ny = 100\n";

        let tokens_v1 = tokenize_source(code_v1);
        let tokens_v2 = tokenize_source(code_v2);

        // Assert that the tokens of unrelated code segment "y = 100" are identical
        // verifying no index drift for downstream elements.
        let tail_v1: Vec<_> = tokens_v1.iter().filter(|t| t.contains("y") || t.contains("100")).collect();
        let tail_v2: Vec<_> = tokens_v2.iter().filter(|t| t.contains("y") || t.contains("100")).collect();

        assert_eq!(tail_v1, tail_v2, "Token-level boundaries shifted inappropriately under unrelated spacing changes!");
    }

    #[test]
    fn test_state_transition_kill_crdt() {
        let mut rga_a = RGADocument::new("replica_a".to_string());

        // Step 1: Base valid AST state
        let code_base = "x = 42\n";
        let tokens_base = tokenize_source(code_base);
        rga_a.replace_tokens(&tokens_base);

        let mut rga_b = rga_a.clone();
        rga_b.replica_id = "replica_b".to_string();

        assert_eq!(rga_a.get_text(), code_base);

        let id_x_pre = rga_a.elements.iter().find(|(_, elem)| elem.value == "x").unwrap().1.id.clone();
        let id_42_pre = rga_a.elements.iter().find(|(_, elem)| elem.value == "42").unwrap().1.id.clone();

        // Step 2: Replica A enters syntax error state (SYNTAX_BREAK)
        let code_a_break = "x = 42 + \n"; // Invalid AST, triggers fallback
        let tokens_a_break = tokenize_source(code_a_break);
        let ops_a = rga_a.replace_tokens(&tokens_a_break);

        // Verify base elements' IDs are preserved on A despite fallback tokenization
        let id_x_break = rga_a.elements.iter().find(|(_, elem)| elem.value == "x" && !elem.deleted).unwrap().1.id.clone();
        let id_42_break = rga_a.elements.iter().find(|(_, elem)| elem.value == "42" && !elem.deleted).unwrap().1.id.clone();
        assert_eq!(id_x_pre, id_x_break);
        assert_eq!(id_42_pre, id_42_break);

        // Step 3: Replica B concurrently edits unrelated downstream statement (CONCURRENT_MERGE)
        let code_b_edit = "x = 42\ny = 100\n";
        let tokens_b_edit = tokenize_source(code_b_edit);
        let ops_b = rga_b.replace_tokens(&tokens_b_edit);

        let id_y_b = rga_b.elements.iter().find(|(_, elem)| elem.value == "y").unwrap().1.id.clone();
        let id_100_b = rga_b.elements.iter().find(|(_, elem)| elem.value == "100").unwrap().1.id.clone();

        // Step 4: Merge concurrent operations
        rga_a.apply_operations(&ops_b);
        rga_b.apply_operations(&ops_a);

        // Verify convergent, zero-text-loss merge
        assert_eq!(rga_a.get_text(), "x = 42 + \ny = 100\n");
        assert_eq!(rga_b.get_text(), "x = 42 + \ny = 100\n");

        // Verify downstream IDs are preserved on A during merge
        let id_y_a_merged = rga_a.elements.iter().find(|(_, elem)| elem.value == "y" && !elem.deleted).unwrap().1.id.clone();
        let id_100_a_merged = rga_a.elements.iter().find(|(_, elem)| elem.value == "100" && !elem.deleted).unwrap().1.id.clone();
        assert_eq!(id_y_b, id_y_a_merged);
        assert_eq!(id_100_b, id_100_a_merged);

        // Step 5: Replica A completes syntax and recovers to a valid AST (VALID_AST_RECOVERY)
        let code_a_recovery = "x = 42 + 8\ny = 100\n";
        let tokens_a_recovery = tokenize_source(code_a_recovery);
        let _ops_a_recovery = rga_a.replace_tokens(&tokens_a_recovery);

        // Verify text recovered perfectly
        assert_eq!(rga_a.get_text(), code_a_recovery);

        // Verify downstream ElementIds survived completely without shift or drift post-recovery!
        let id_y_a_recovered = rga_a.elements.iter().find(|(_, elem)| elem.value == "y" && !elem.deleted).unwrap().1.id.clone();
        let id_100_a_recovered = rga_a.elements.iter().find(|(_, elem)| elem.value == "100" && !elem.deleted).unwrap().1.id.clone();
        assert_eq!(id_y_b, id_y_a_recovered);
        assert_eq!(id_100_b, id_100_a_recovered);
    }
}
