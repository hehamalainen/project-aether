with open("crates/server/src/db_postgres.rs", "r") as f:
    content = f.read()

start_idx = content.find("pub async fn request_promotion(")
end_idx = content.find("\n// End of handlers\n", start_idx)
if end_idx == -1:
    end_idx = len(content)

with open("crates/server/src/main.rs", "r") as f:
    main_content = f.read()

m_start = main_content.find("async fn request_promotion(")
m_end = main_content.find("\n#[cfg(feature = \"db-sqlite\")]\nasync fn list_promotions(", m_start)

# The signature in pg is slightly different: pub async fn request_promotion(...
m_func = "pub " + main_content[m_start:m_end]

# We need to change ? to $N for PostgreSQL bindings
# We can do this manually by replacing standard queries, or just rewrite the PG version.
# Let's write a simple regex-based parameter replacer for the known queries.
import re
def replace_binds(sql):
    count = 0
    def rep(m):
        nonlocal count
        count += 1
        return f"${count}"
    return re.sub(r"\?", rep, sql)

lines = m_func.split("\n")
new_lines = []
for line in lines:
    if 'sqlx::query(' in line or 'sqlx::query_as(' in line or 'sqlx::query_as::<' in line:
        match = re.search(r'(".*?")', line)
        if match:
            old_str = match.group(1)
            new_str = replace_binds(old_str)
            line = line.replace(old_str, new_str)
    new_lines.append(line)

# Handle CAS update query
pg_func = "\n".join(new_lines)
# In PostgreSQL, `head_id IS ?` might cause issues if ? is NULL because of type inference, but in SQLite we used `IS ?`. 
# In pg, we previously wrote: `(head_id = $5 OR ($5::text IS NULL AND head_id IS NULL))`
pg_func = pg_func.replace("head_id IS $5", "(head_id = $5 OR ($5::text IS NULL AND head_id IS NULL))")

new_content = content[:start_idx] + pg_func + content[end_idx:]

with open("crates/server/src/db_postgres.rs", "w") as f:
    f.write(new_content)
