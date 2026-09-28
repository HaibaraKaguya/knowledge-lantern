"""Evidence-only gaps: no model inference is presented as a learner fact."""
from __future__ import annotations
from .models import KnowledgeGap

class GapService:
    def __init__(self, store):
        self.store = store
    def gaps_for(self, concept_id: str) -> list[KnowledgeGap]:
        rows = self.store.conn.execute("""WITH related(id) AS (SELECT ? UNION SELECT to_concept_id FROM study_concept_edges WHERE from_concept_id=?), facts AS (SELECT c.concept_id,c.name,COUNT(l.record_id) total,SUM(CASE WHEN r.mastery IN ('weak','unknown') OR l.error_type<>'' THEN 1 ELSE 0 END) weak FROM related x JOIN study_concepts c ON c.concept_id=x.id LEFT JOIN study_record_concepts l ON l.concept_id=c.concept_id LEFT JOIN study_records r ON r.record_id=l.record_id GROUP BY c.concept_id,c.name) SELECT * FROM facts WHERE total>0 AND weak>0 ORDER BY weak DESC,total DESC,name""", (concept_id, concept_id)).fetchall()
        return [KnowledgeGap(row["concept_id"], row["name"], row["total"], row["weak"], f"{row['weak']}/{row['total']} saved records are weak, unknown, or tagged with an error.") for row in rows]
