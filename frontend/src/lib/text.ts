// Utilidades de tipografia no navegador (apenas para posicionar a caixa de edição;
// o texto final é sempre gerado pelo servidor no PDF).

export function cssFamily(serif?: boolean, mono?: boolean): string {
  if (mono) return '"Courier New", "Liberation Mono", Courier, monospace';
  if (serif) return '"Times New Roman", "Liberation Serif", Times, serif';
  return 'Helvetica, Arial, "Liberation Sans", sans-serif';
}

let canvas: HTMLCanvasElement | null = null;

/** Largura aproximada (em pontos) de um texto. */
export function measureText(text: string, size: number, opts: { bold?: boolean; italic?: boolean; serif?: boolean; mono?: boolean }): number {
  canvas ??= document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  if (!ctx) return text.length * size * 0.5;
  ctx.font = `${opts.italic ? "italic " : ""}${opts.bold ? "bold " : ""}${size}px ${cssFamily(opts.serif, opts.mono)}`;
  return Math.max(...text.split("\n").map((l) => ctx.measureText(l).width), 0);
}
