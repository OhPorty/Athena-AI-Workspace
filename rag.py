import os
import glob
import sqlite3
import hashlib
from typing import List, Dict, Any

class SimpleCodeRAG:
    def __init__(self, db_path: str = "rag_index.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                filepath TEXT,
                content TEXT,
                file_hash TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def index_codebase(self, root_dir: str = "."):
        exclude_dirs = {".git", "venv", "node_modules", "file_backups", "__pycache__"}
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        for filepath in glob.glob(os.path.join(root_dir, "**/*.*"), recursive=True):
            parts = filepath.split(os.sep)
            if any(d in parts for d in exclude_dirs):
                continue
            if not (filepath.endswith(".py") or filepath.endswith(".md") or filepath.endswith(".json")):
                continue
            
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
            
            # Clear this file's existing chunks first -- otherwise a file
            # that shrinks or gets restructured leaves stale chunk rows
            # behind forever, since chunk ids are filepath:line-index and
            # a shorter file never overwrites the higher-index chunks a
            # longer previous version created.
            cursor.execute('DELETE FROM chunks WHERE filepath = ?', (filepath,))
            
            # Simple line/paragraph chunking
            lines = content.splitlines()
            chunk_size = 50
            for i in range(0, len(lines), chunk_size):
                chunk_lines = lines[i:i+chunk_size]
                chunk_text = "\n".join(chunk_lines)
                if not chunk_text.strip():
                    continue
                chunk_id = hashlib.md5(f"{filepath}:{i}".encode("utf-8")).hexdigest()
                
                cursor.execute('''
                    INSERT OR REPLACE INTO chunks (id, filepath, content, file_hash)
                    VALUES (?, ?, ?, ?)
                ''', (chunk_id, filepath, chunk_text, file_hash))

        conn.commit()
        conn.close()

    def search(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Simple keyword matching search for demonstration/robustness
        query_terms = query.lower().split()
        if not query_terms:
            conn.close()
            return []

        cursor.execute("SELECT id, filepath, content FROM chunks")
        rows = cursor.fetchall()
        conn.close()

        scored = []
        for chunk_id, filepath, content in rows:
            score = 0
            lower_content = content.lower()
            for term in query_terms:
                if term in lower_content:
                    score += lower_content.count(term)
            if score > 0:
                scored.append({"filepath": filepath, "content": content, "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]
