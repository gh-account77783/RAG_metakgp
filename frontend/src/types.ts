export type EngineId = "graphmind" | "tree_navigation" | "multimodal";
export interface Engine {
  engine_id: EngineId;
  display_name: string;
  implemented: boolean;
  configured: boolean;
  available: boolean;
  max_documents: number;
  requires_document: boolean;
}
export interface SourceDocument {
  document_id: string;
  version_id: string;
  display_name: string;
  media_type: string;
  engines: Record<EngineId, {state: string; ready: boolean}>;
}
export interface Catalog { engines: Engine[]; documents: SourceDocument[] }
export interface Citation {
  citation_id: string;
  document_id: string;
  version_id: string;
  display_name: string;
  locator: string;
  excerpt: string;
  source_title?: string;
  page_number?: number;
}
export interface Answer { outcome: "answer" | "insufficient_evidence"; answer: string; citations: Citation[] }
