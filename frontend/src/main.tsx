import React from "react";
import { createRoot } from "react-dom/client";
import { ApiClient, ApiError } from "./api";
import { AnswerPanel, DocumentPicker, EnginePicker, SourcePanel } from "./components";
import type { Preview } from "./components";
import type { Answer, Catalog, Citation, EngineId } from "./types";
import "./style.css";

const api = new ApiClient();
const authLost = (error: unknown) => error instanceof ApiError && [401, 403].includes(error.status);
const message = (error: unknown) => error instanceof ApiError ? error.message : "Request failed. Please retry.";

function Reader({email}: {email: string}) {
  const [catalog, setCatalog] = React.useState<Catalog | null>(null);
  const [engineId, setEngineId] = React.useState<EngineId>("graphmind");
  const [selected, setSelected] = React.useState<string[]>([]);
  const [question, setQuestion] = React.useState("");
  const [answer, setAnswer] = React.useState<Answer | null>(null);
  const [status, setStatus] = React.useState("Loading documents…");
  const [busy, setBusy] = React.useState(false);
  const [signedOut, setSignedOut] = React.useState(false);
  const [preview, setPreview] = React.useState<Preview | null>(null);
  const [sourceBusy, setSourceBusy] = React.useState(false);
  const work = React.useRef<{id: number; controller?: AbortController}>({id: 0});
  const sourceWork = React.useRef<{id: number; controller?: AbortController; url?: string}>({id: 0});
  const engine = catalog?.engines.find(item => item.engine_id === engineId);

  function clearSource() {
    sourceWork.current.controller?.abort();
    if (sourceWork.current.url) URL.revokeObjectURL(sourceWork.current.url);
    sourceWork.current = {id: sourceWork.current.id + 1};
    setPreview(null); setSourceBusy(false);
  }
  function cancel() {
    work.current.controller?.abort();
    work.current = {id: work.current.id + 1};
    setBusy(false);
  }
  function reset() { cancel(); clearSource(); setAnswer(null); }
  function failed(error: unknown) {
    if (authLost(error)) { reset(); setSignedOut(true); }
    setStatus(message(error));
  }

  React.useEffect(() => {
    const controller = new AbortController();
    api.post<Catalog>("/api/catalog", {}, controller.signal).then(value => {
      setCatalog(value);
      const initial = value.engines.find(item => item.implemented && item.available &&
        value.documents.some(document => document.engines[item.engine_id].ready)) ??
        value.engines.find(item => item.implemented && item.available);
      if (initial) setEngineId(initial.engine_id);
      setStatus(value.documents.length ? "Ready" : "No documents are available yet.");
    }).catch(error => { if (!controller.signal.aborted) failed(error); });
    return () => { controller.abort(); work.current.controller?.abort(); sourceWork.current.controller?.abort();
      if (sourceWork.current.url) URL.revokeObjectURL(sourceWork.current.url); };
  }, []);

  async function ask(event: React.FormEvent) {
    event.preventDefault();
    if (!question.trim() || busy || signedOut) return;
    reset();
    const controller = new AbortController();
    const id = work.current.id;
    work.current.controller = controller;
    setBusy(true); setStatus("Finding evidence and preparing an answer…");
    try {
      const result = await api.post<Answer>("/api/ask", {question, engine_id: engineId, document_ids: selected}, controller.signal);
      if (id !== work.current.id || controller.signal.aborted) return;
      if (!["answer", "insufficient_evidence"].includes(result.outcome) || !Array.isArray(result.citations))
        throw new Error("Invalid answer response");
      setAnswer(result); setStatus(result.outcome === "answer" ? "Answered" : "No supported answer");
    } catch (error) { if (!controller.signal.aborted && id === work.current.id) failed(error); }
    finally { if (id === work.current.id) setBusy(false); }
  }

  async function openSource(source: Citation) {
    clearSource();
    const controller = new AbortController();
    const id = sourceWork.current.id;
    sourceWork.current.controller = controller;
    setSourceBusy(true);
    const base = `/documents/${encodeURIComponent(source.document_id)}`;
    try {
      const response = await api.request(base + (source.page_number ? `/pages/${source.page_number}` : "") +
        `?version_id=${encodeURIComponent(source.version_id)}`, "GET", undefined, controller.signal);
      if (source.page_number) {
        if (!response.headers.get("content-type")?.startsWith("image/png")) throw new Error("Invalid page response");
        const blob = await response.blob();
        if (id !== sourceWork.current.id || controller.signal.aborted) return;
        const url = URL.createObjectURL(blob); sourceWork.current.url = url;
        setPreview({source, imageUrl: url});
      } else {
        const text = await response.text();
        if (id === sourceWork.current.id && !controller.signal.aborted) setPreview({source, text});
      }
    } catch (error) { if (!controller.signal.aborted && id === sourceWork.current.id) failed(error); }
    finally { if (id === sourceWork.current.id) setSourceBusy(false); }
  }

  async function download() {
    if (!preview) return;
    const {source} = preview;
    const controller = new AbortController();
    sourceWork.current.controller?.abort();
    sourceWork.current.controller = controller;
    const id = sourceWork.current.id;
    try {
      const response = await api.request(`/documents/${encodeURIComponent(source.document_id)}/original?version_id=${encodeURIComponent(source.version_id)}`, "GET", undefined, controller.signal);
      const blob = await response.blob();
      if (id !== sourceWork.current.id || controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = source.display_name;
      anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { if (!controller.signal.aborted && id === sourceWork.current.id) failed(error); }
  }

  async function logout() {
    reset();
    try { await api.post("/logout", {}); location.replace("/"); }
    catch (error) { setStatus("Sign-out could not be confirmed. Please retry. " + message(error)); }
  }
  const readyCount = catalog?.documents.filter(item => item.engines[engineId].ready).length ?? 0;
  const canAsk = !!engine?.available && readyCount > 0 && (!engine.requires_document || selected.length === 1) && !signedOut;
  return <div className="reader-shell"><header className="reader-header"><div><a className="brand" href="/">GraphMind</a>
    <p className="muted">Your documents, with supporting evidence.</p></div><div className="identity"><span>{email}</span>
    <button type="button" className="secondary" onClick={() => void logout()}>Sign out</button></div></header>
    <div className="reader-layout"><nav className="selection-panel" aria-label="Knowledge set">
      <EnginePicker engines={catalog?.engines ?? []} value={engineId} onChange={value => {
        reset(); setEngineId(value); setSelected([]); setStatus("Ready");
      }} />
      <DocumentPicker documents={catalog?.documents ?? []} engine={engine} selected={selected} onChange={value => {
        reset(); setSelected(value); setStatus("Ready");
      }} />
    </nav><main className="workspace"><form id="ask-form" onSubmit={event => void ask(event)}>
      <label htmlFor="question">Question</label><textarea id="question" maxLength={2000} required value={question}
        disabled={signedOut} readOnly={busy} onChange={event => setQuestion(event.target.value)} placeholder="What would you like to know?" />
      <div className="actions"><button type="submit" disabled={!canAsk || busy || !question.trim()}>Ask</button>
        {busy && <button type="button" className="secondary" onClick={() => { cancel(); setStatus("Cancelled"); }}>Cancel</button>}
        <button type="button" className="secondary" onClick={() => { reset(); setQuestion(""); setStatus("Ready"); }}>Clear</button></div>
    </form><p id="status" className="status" role="status">{status}</p>{signedOut && <a href="/login">Sign in again</a>}
      <AnswerPanel answer={answer} onSource={source => void openSource(source)} />
    </main><SourcePanel preview={preview} loading={sourceBusy} onClose={clearSource} onDownload={() => void download()} /></div>
  </div>;
}

const mount = document.getElementById("graphmind-root");
if (mount) createRoot(mount).render(<Reader email={mount.dataset.email ?? ""} />);
const resume = document.getElementById("resume");
if (resume) void api.refresh().then(() => {
  const target = new URL(resume.dataset.returnTo ?? "/", location.origin);
  if (target.origin !== location.origin) throw new Error("Invalid return path");
  location.replace(target.pathname + target.search);
}).catch(() => {
  const status = document.getElementById("status");
  if (status) status.textContent = "Session renewal failed. Retry or sign in again.";
});
