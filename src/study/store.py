"""SQLite durable store. Images remain files; SQLite contains metadata and text only."""
from __future__ import annotations
import json, sqlite3
from pathlib import Path
from .models import PhotoRecord, OCRText, Concept, RecordConceptLink, ConceptEdge
_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_records (record_id TEXT PRIMARY KEY, image_path TEXT NOT NULL, image_hash TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', mastery TEXT NOT NULL DEFAULT 'unknown', starred INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS study_records_image_hash ON study_records(image_hash);
CREATE TABLE IF NOT EXISTS study_ocr (record_id TEXT PRIMARY KEY REFERENCES study_records(record_id) ON DELETE CASCADE, text TEXT NOT NULL, formula_text TEXT NOT NULL DEFAULT '', engine TEXT NOT NULL DEFAULT '', confidence REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS study_fts USING fts5(record_id UNINDEXED, searchable);
CREATE TABLE IF NOT EXISTS study_concepts (concept_id TEXT PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL DEFAULT 'math', aliases_json TEXT NOT NULL DEFAULT '[]', description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS study_record_concepts (record_id TEXT NOT NULL REFERENCES study_records(record_id) ON DELETE CASCADE, concept_id TEXT NOT NULL REFERENCES study_concepts(concept_id) ON DELETE CASCADE, confidence REAL NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'user', error_type TEXT NOT NULL DEFAULT '', PRIMARY KEY(record_id, concept_id));
CREATE TABLE IF NOT EXISTS study_concept_edges (from_concept_id TEXT NOT NULL REFERENCES study_concepts(concept_id), to_concept_id TEXT NOT NULL REFERENCES study_concepts(concept_id), relation TEXT NOT NULL DEFAULT 'prerequisite', weight REAL NOT NULL DEFAULT 1, PRIMARY KEY(from_concept_id, to_concept_id, relation));
CREATE INDEX IF NOT EXISTS study_links_concept ON study_record_concepts(concept_id);
CREATE INDEX IF NOT EXISTS study_edges_from ON study_concept_edges(from_concept_id);
"""
class StudyStore:
 def __init__(self, db_path: str | Path | None = None):
  if db_path: Path(db_path).parent.mkdir(parents=True, exist_ok=True)
  self.conn=sqlite3.connect(str(db_path) if db_path else ':memory:'); self.conn.row_factory=sqlite3.Row; self.conn.execute('PRAGMA foreign_keys = ON'); self.conn.executescript(_SCHEMA)
 def close(self): self.conn.close()
 def add_record(self, item: PhotoRecord):
  self.conn.execute('INSERT OR REPLACE INTO study_records VALUES (?,?,?,?,?,?,?,?,?)',(item.record_id,item.image_path,item.image_hash,item.created_at,item.title,item.note,item.status,item.mastery,int(item.starred))); self.conn.commit()
 def add_ocr(self,item: OCRText):
  self.conn.execute('INSERT OR REPLACE INTO study_ocr VALUES (?,?,?,?,?)',(item.record_id,item.text,item.formula_text,item.engine,item.confidence)); self.conn.execute('DELETE FROM study_fts WHERE record_id=?',(item.record_id,)); self.conn.execute('INSERT INTO study_fts VALUES (?,?)',(item.record_id,' '.join((item.text,item.formula_text)))); self.conn.commit()
 def add_concept(self,item: Concept): self.conn.execute('INSERT OR REPLACE INTO study_concepts VALUES (?,?,?,?,?)',(item.concept_id,item.name,item.subject,json.dumps(item.aliases,ensure_ascii=False),item.description)); self.conn.commit()
 def link(self,item: RecordConceptLink): self.conn.execute('INSERT OR REPLACE INTO study_record_concepts VALUES (?,?,?,?,?)',(item.record_id,item.concept_id,item.confidence,item.source,item.error_type)); self.conn.commit()
 def add_edge(self,item: ConceptEdge): self.conn.execute('INSERT OR REPLACE INTO study_concept_edges VALUES (?,?,?,?)',(item.from_concept_id,item.to_concept_id,item.relation,item.weight)); self.conn.commit()
