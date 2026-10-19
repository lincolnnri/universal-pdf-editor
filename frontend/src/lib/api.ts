// Cliente da API do backend + tipos compartilhados.

export interface PageInfo {
  number: number;
  width: number;
  height: number;
  rotation: number;
}

export interface DocInfo {
  id: string;
  filename: string;
  pageCount: number;
  pages: PageInfo[];
  signed: boolean;
  encrypted: boolean;
  warnings: string[];
}

export interface TextElement {
  id: string;
  page: number;
  kind: "native" | "ocr";
  text: string;
  bbox: [number, number, number, number];
  size: number;
  color: string;
  font: string;
  bold: boolean;
  italic: boolean;
  serif: boolean;
  mono: boolean;
  align: "left" | "right" | "center" | "justify";
  angle: number;
  maxExtent: number;
  width: number;
  bgColor: string | null;
  multiline: boolean;
  lineHeight: number | null;
}

export interface ImageElement {
  id: string;
  bbox: [number, number, number, number];
}

export interface FormField {
  id: string;
  name: string;
  type: "text" | "checkbox" | "radio" | "choice";
  value: string;
  bbox: [number, number, number, number];
  readOnly: boolean;
  multiline: boolean;
  size: number;
  options?: string[];
  onState?: string;
}

export interface PageAnalysis {
  number: number;
  width: number;
  height: number;
  rotation: number;
  mode: "native" | "scanned" | "ocr_layer" | "mixed" | "empty";
  needsOcr: boolean;
  ocrDone: boolean;
  notes: string[];
  elements: TextElement[];
  images: ImageElement[];
  fields: FormField[];
}

export type Align = "left" | "right" | "center" | "justify";

export type Operation =
  | {
      type: "edit_text";
      page: number;
      elementId: string;
      text: string;
      size?: number;
      color?: string;
      bold?: boolean;
      italic?: boolean;
      align?: Align;
    }
  | { type: "delete_element"; page: number; elementId: string }
  | {
      type: "add_text";
      page: number;
      x: number;
      y: number;
      text: string;
      size: number;
      color: string;
      bold?: boolean;
      italic?: boolean;
      fontFamily?: "sans" | "serif" | "mono";
      /** identificador local (não enviado ao servidor) */
      localId?: string;
    }
  | { type: "set_field"; page: number; elementId: string; value: string | boolean }
  | { type: "add_image"; page: number; rect: [number, number, number, number]; imageId: string; localId?: string; previewUrl?: string };

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

export interface ExportReport {
  ok: boolean;
  checks: Check[];
  errors: string[];
  warnings: string[];
  ops: { index: number; type: string; page: number; mode: string; font: string | null; warnings: string[] }[];
  pages: number;
  sizeBytes: number;
  durationMs: number;
  downloadUrl?: string;
  filename?: string;
}

export class ApiError extends Error {
  code: string;
  status: number;
  report?: ExportReport;
  constructor(status: number, code: string, message: string, report?: ExportReport) {
    super(message);
    this.status = status;
    this.code = code;
    this.report = report;
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (res.ok) return (await res.json()) as T;
  let code = "http_" + res.status;
  let message = `Erro ${res.status} ao comunicar com o servidor.`;
  let report: ExportReport | undefined;
  try {
    const body = await res.json();
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      report = body.error.report;
    } else if (body?.detail) {
      message = typeof body.detail === "string" ? body.detail : "Requisição inválida.";
    }
  } catch {
    /* corpo não-JSON */
  }
  throw new ApiError(res.status, code, message, report);
}

/** Remove campos locais antes de enviar ao servidor. */
export function serializeOps(ops: Operation[]): object[] {
  return ops.map((op) => {
    const { localId: _l, previewUrl: _p, ...rest } = op as Operation & { localId?: string; previewUrl?: string };
    return rest;
  });
}

export const api = {
  async upload(file: File, password?: string): Promise<DocInfo> {
    const fd = new FormData();
    fd.append("file", file);
    if (password) fd.append("password", password);
    return handle(await fetch("/api/documents", { method: "POST", body: fd }));
  },
  async analysis(docId: string, page: number, ocr: "auto" | "force" | "off" = "auto"): Promise<PageAnalysis> {
    return handle(await fetch(`/api/documents/${docId}/pages/${page}/analysis?ocr=${ocr}`));
  },
  pageImageUrl(docId: string, page: number, scale: number): string {
    return `/api/documents/${docId}/pages/${page}/image?scale=${scale}`;
  },
  async preview(docId: string, page: number, ops: Operation[], scale: number, signal?: AbortSignal): Promise<Blob> {
    const res = await fetch(`/api/documents/${docId}/pages/${page}/preview?scale=${scale}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ops: serializeOps(ops) }),
      signal,
    });
    if (!res.ok) await handle(res);
    return res.blob();
  },
  async uploadImage(docId: string, file: File): Promise<{ imageId: string; width: number; height: number }> {
    const fd = new FormData();
    fd.append("file", file);
    return handle(await fetch(`/api/documents/${docId}/images`, { method: "POST", body: fd }));
  },
  async exportPdf(docId: string, ops: Operation[], acknowledgeSignature: boolean,
                  pages: number[] | null = null): Promise<ExportReport> {
    return handle(
      await fetch(`/api/documents/${docId}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ops: serializeOps(ops), acknowledgeSignature, ...(pages ? { pages } : {}) }),
      }),
    );
  },
  async close(docId: string): Promise<void> {
    await fetch(`/api/documents/${docId}`, { method: "DELETE" }).catch(() => undefined);
  },
};
