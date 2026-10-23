use crdt::document_store::DocumentStore;

#[test]
fn test_vacuum() {
    let mut store = DocumentStore::new("repo".to_string());
    store.apply_full_file("file1.py", "content 1", "prop1");
    store.apply_full_file("file2.py", "content 2", "prop2");
    
    let meet = Some(crdt::causality::VersionVector::new());
    for file_doc in store.documents.values_mut() {
        file_doc.document.vacuum_tombstones_bounded(meet.as_ref());
    }
    
    assert_eq!(store.documents.len(), 2);
    
    let content1 = store.documents.get("file1.py").unwrap().document.get_text();
    assert_eq!(content1, "content 1");
}
