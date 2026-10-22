import React from "react";
import type { Answer, Catalog, Citation, Engine, EngineId, SourceDocument } from "./types";

export function EnginePicker({engines, value, onChange}: {
  engines: Engine[]; value: EngineId; onChange: (value: EngineId) => void;
}) {
  return <label>Retrieval method<select value={value} onChange={event => onChange(event.target.value as EngineId)}>
    {engines.map(engine => <option key={engine.engine_id} value={engine.engine_id}
      disabled={!engine.implemented || !engine.available}>
      {engine.display_name}{!engine.configured ? " — not configured" : !engine.available ? " — unavailable" : ""}
    </option>)}
  </select></label>;
}

export function DocumentPicker({documents, engine, selected, onChange}: {
  documents: SourceDocument[]; engine?: Engine; selected: string[]; onChange: (value: string[]) => void;
}) {
  const [filter, setFilter] = React.useState("");
  const visible = documents.filter(document => document.display_name.toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
  const key = engine?.engine_id ?? "graphmind";
  return <section className="documents" aria-label="Document selection">
    <div className="section-heading"><h2>Documents</h2><span>{documents.filter(document => document.engines[key].ready).length} ready</span></div>
    <p className="muted">{engine?.requires_document ? "Choose one ready PDF." : "Choose up to 16 documents, or search all ready documents."}</p>
    <input aria-label="Filter documents" type="search" placeholder="Find a document" value={filter}
      onChange={event => setFilter(event.target.value)} />
    <div className="document-list">
      {visible.map(document => <label className="document-row" key={document.document_id}>
        <input type={engine?.max_documents === 1 ? "radio" : "checkbox"} name="documents"
          checked={selected.includes(document.document_id)} disabled={!document.engines[key].ready}
          onChange={event => onChange(engine?.max_documents === 1 ? [document.document_id]
            : event.target.checked ? [...selected, document.document_id].slice(0, 16)
            : selected.filter(identifier => identifier !== document.document_id))} />
        <span>{document.display_name}<small>{document.engines[key].state.replaceAll("_", " ")}</small></span>
      </label>)}
      {!visible.length && <p className="muted">No matching documents.</p>}
    </div>
    {selected.length > 0 && <button type="button" className="secondary" onClick={() => onChange([])}>Clear selection</button>}
  </section>;
}

export function AnswerPanel({answer, onSource}: {answer: Answer | null; onSource: (source: Citation) => void}) {
  if (!answer) return <section className="empty-state"><h2>Ask your knowledge set</h2>
    <p>Choose a retrieval method and ask a question. Answers include supporting sources.</p></section>;
  return <section className="answer-panel" aria-label="Answer"><h2>{answer.outcome === "answer" ? "Answer" : "No supported answer"}</h2>
    <div className="answer-text">{answer.answer}</div>
    {!!answer.citations.length && <><h3>Sources</h3><ol className="citations">{answer.citations.map(source =>
      <li key={source.citation_id}><button type="button" className="source-link" onClick={() => onSource(source)}>
        {source.source_title || source.display_name} · {source.locator}</button><p>{source.excerpt}</p></li>)}</ol></>}
  </section>;
}

export interface Preview {source: Citation; imageUrl?: string; text?: string}
export function SourcePanel({preview, loading, onClose, onDownload}: {
  preview: Preview | null; loading: boolean; onClose: () => void; onDownload: () => void;
}) {
  if (!preview && !loading) return null;
  return <aside className="source-panel" aria-label="Source preview"><div className="section-heading"><h2>Source</h2>
    <button type="button" className="secondary" onClick={onClose}>Close</button></div>
    {loading ? <p role="status">Loading source…</p> : preview && <>
      <h3>{preview.source.display_name}</h3><p className="muted">{preview.source.locator}</p>
      {preview.imageUrl ? <img src={preview.imageUrl} alt={`Original ${preview.source.locator} of ${preview.source.display_name}`} />
        : <pre>{preview.text}</pre>}
      <button type="button" onClick={onDownload}>Download original</button>
    </>}
  </aside>;
}
