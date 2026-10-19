// Estado centralizado das alterações, com desfazer/refazer.
// Toda alteração do usuário vira uma lista de operações; cada commit é um passo do histórico.
import { useCallback, useReducer } from "react";
import type { Operation } from "./api";

interface State {
  past: Operation[][];
  present: Operation[];
  future: Operation[][];
}

type Action =
  | { type: "commit"; ops: Operation[] }
  | { type: "undo" }
  | { type: "redo" }
  | { type: "reset" };

const LIMIT = 200;

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "commit":
      if (action.ops === state.present) return state;
      return { past: [...state.past, state.present].slice(-LIMIT), present: action.ops, future: [] };
    case "undo":
      if (!state.past.length) return state;
      return {
        past: state.past.slice(0, -1),
        present: state.past[state.past.length - 1],
        future: [state.present, ...state.future],
      };
    case "redo":
      if (!state.future.length) return state;
      return { past: [...state.past, state.present], present: state.future[0], future: state.future.slice(1) };
    case "reset":
      return { past: [], present: [], future: [] };
  }
}

export function useOpsHistory() {
  const [state, dispatch] = useReducer(reducer, { past: [], present: [], future: [] });
  return {
    ops: state.present,
    canUndo: state.past.length > 0,
    canRedo: state.future.length > 0,
    commit: useCallback((ops: Operation[]) => dispatch({ type: "commit", ops }), []),
    undo: useCallback(() => dispatch({ type: "undo" }), []),
    redo: useCallback(() => dispatch({ type: "redo" }), []),
    reset: useCallback(() => dispatch({ type: "reset" }), []),
  };
}

let counter = 0;
export function localId(prefix: string): string {
  counter += 1;
  return `${prefix}-${Date.now().toString(36)}-${counter}`;
}

/** Substitui (ou remove) a operação associada a um elemento existente. */
export function setElementOp(ops: Operation[], elementId: string, op: Operation | null): Operation[] {
  const rest = ops.filter((o) => !("elementId" in o) || o.elementId !== elementId);
  return op ? [...rest, op] : rest;
}

export function opForElement(ops: Operation[], elementId: string): Operation | undefined {
  return ops.find((o) => "elementId" in o && o.elementId === elementId);
}

export function updateLocal(ops: Operation[], id: string, patch: Partial<Operation>): Operation[] {
  return ops.map((o) => ("localId" in o && o.localId === id ? ({ ...o, ...patch } as Operation) : o));
}

export function removeLocal(ops: Operation[], id: string): Operation[] {
  return ops.filter((o) => !("localId" in o) || o.localId !== id);
}
