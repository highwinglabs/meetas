"""Local vector store over transcript segments (Phase 5).

Vectors are stored as packed little-endian float32 BLOBs in the
``segment_embedding`` table (one row per meeting/segment/model). Queries load
the (small, personal-scale) vectors into numpy and compute cosine scores there.
Everything is offline; there is no external vector database.
"""
from __future__ import annotations

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from core.search.embeddings import EmbeddingEngine
from core.store.models import Meeting, SegmentEmbedding, TranscriptSegment
from core.logging_setup import get_logger

log = get_logger("ma.vectorstore")


def _pack(vec: np.ndarray) -> bytes:
    return np.ascontiguousarray(vec, dtype=np.float32).tobytes()


def _unpack(blob: bytes) -> np.ndarray:
    dim = len(blob) // 4
    return np.frombuffer(blob, dtype=np.float32).copy()


def embed_meeting(session: Session, meeting_id: str,
                  engine: EmbeddingEngine) -> int:
    """(Re)compute + store embeddings for every segment of a meeting.

    Idempotent: existing rows for (meeting, model) are removed first, so changing
    the embedder simply rebuilds the index. Returns the number of stored vectors.
    """
    segs = session.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.start_s)
    ).all()
    if not segs:
        return 0
    texts = [f"{s.speaker_id or ''} {s.text or ''}".strip() for s in segs]
    vecs = engine.embed(texts)
    model = engine.name

    session.execute(
        delete(SegmentEmbedding).where(
            SegmentEmbedding.meeting_id == meeting_id,
            SegmentEmbedding.model == model,
        )
    )
    for seg, vec in zip(segs, vecs):
        session.add(SegmentEmbedding(
            meeting_id=meeting_id, seg_id=seg.id, model=model,
            dim=int(engine.dim), vector=_pack(vec),
        ))
    # The caller owns the transaction.  Committing here would make the vector
    # replacement visible before the corresponding job/meeting state update
    # and could leave those records inconsistent after a crash.
    log.info("embed_meeting meeting=%s rows=%s model=%s", meeting_id[:8], len(vecs), model)
    return len(vecs)


def upsert_segment_embedding(session: Session, meeting_id: str, seg_id: str,
                             engine: EmbeddingEngine) -> bool:
    """Refresh the vector for a single segment (after an edit). No-op when the
    meeting has no index yet (the first ``embed_meeting`` build will include it)."""
    seg = session.get(TranscriptSegment, seg_id)
    if seg is None or seg.meeting_id != meeting_id:
        return False
    model = engine.name
    if session.scalar(select(SegmentEmbedding.id).where(
            SegmentEmbedding.meeting_id == meeting_id,
            SegmentEmbedding.model == model).limit(1)) is None:
        return False  # index not built for this meeting/model yet
    text = f"{seg.speaker_id or ''} {seg.text or ''}".strip()
    vec = engine.embed_one(text)
    existing = session.scalar(select(SegmentEmbedding).where(
        SegmentEmbedding.meeting_id == meeting_id,
        SegmentEmbedding.seg_id == seg_id,
        SegmentEmbedding.model == model))
    if existing is None:
        session.add(SegmentEmbedding(meeting_id=meeting_id, seg_id=seg_id,
                                     model=model, dim=int(engine.dim),
                                     vector=_pack(vec)))
    else:
        existing.vector = _pack(vec)
        existing.dim = int(engine.dim)
    return True


def _load_vectors(session: Session, model: str,
                  meeting_id: str | None,
                  meeting_ids: set[str] | None = None) -> tuple[list[str], np.ndarray]:
    # Join through both ownership links so a corrupted/stale embedding can
    # never surface a segment from another meeting or from the paper bin.
    q = (select(SegmentEmbedding)
         .join(TranscriptSegment, (TranscriptSegment.id == SegmentEmbedding.seg_id)
               & (TranscriptSegment.meeting_id == SegmentEmbedding.meeting_id))
         .join(Meeting, Meeting.id == SegmentEmbedding.meeting_id)
         .where(SegmentEmbedding.model == model, Meeting.deleted_at.is_(None))
         .order_by(SegmentEmbedding.meeting_id, SegmentEmbedding.seg_id))
    if meeting_id is not None:
        q = q.where(SegmentEmbedding.meeting_id == meeting_id)
    elif meeting_ids is not None:
        if not meeting_ids:
            return [], np.zeros((0, 0), dtype=np.float32)
        q = q.where(SegmentEmbedding.meeting_id.in_(list(meeting_ids)))
    rows = session.scalars(q).all()
    # A damaged BLOB must not take down the whole search endpoint.  Integrity
    # checks report these rows; search simply ignores them until re-indexing.
    valid_rows = []
    expected_dim = None
    for row in rows:
        try:
            row_dim = int(row.dim)
            blob = row.vector or b""
            # Establish the common dimensionality only from a fully valid
            # first row. A malformed first row must not poison valid rows.
            valid = row_dim > 0 and len(blob) == row_dim * 4
            if valid:
                candidate = _unpack(blob)
                valid = bool(np.all(np.isfinite(candidate)))
                if valid and expected_dim is None:
                    expected_dim = row_dim
                elif valid:
                    valid = row_dim == expected_dim
        except (TypeError, ValueError, OverflowError):
            valid = False
        if valid:
            valid_rows.append(row)
        else:
            log.warning("invalid_embedding_blob seg=%s model=%s", row.seg_id, model)
    rows = valid_rows
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    ids = [r.seg_id for r in rows]
    mat = np.stack([_unpack(r.vector) for r in rows]).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.clip(norms, 1e-12, None)
    return ids, mat


def vector_search(session: Session, query: str, engine: EmbeddingEngine,
                  limit: int = 25, meeting_id: str | None = None,
                  meeting_ids: set[str] | None = None,
                  model: str | None = None) -> list[dict]:
    """Cosine top-``limit`` over stored segment vectors. Empty when the meeting
    (or index) has no embeddings yet."""
    model = model or engine.name
    ids, mat = _load_vectors(session, model, meeting_id, meeting_ids)
    if mat.size == 0:
        return []
    q = engine.embed_one(query).astype(np.float32)
    qn = float(np.linalg.norm(q))
    if qn <= 0:
        return []
    q = q / qn
    scores = mat @ q
    order = np.argsort(-scores)[: max(0, limit)]
    out = []
    for i in order:
        if scores[i] > 0.0:
            out.append({"seg_id": ids[i], "score": float(scores[i])})
    return out
