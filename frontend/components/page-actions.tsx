'use client';

import { createContext, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';

const Context = createContext<{ slot: HTMLElement | null; setSlot: (node: HTMLElement | null) => void }>({ slot: null, setSlot: () => {} });

/** The page heading belongs to the shell; a view adds its own actions beside the shared refresh button. */
export function PageActionsProvider({ children }: { children: ReactNode }) {
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const value = useMemo(() => ({ slot, setSlot }), [slot]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function PageActionsSlot() {
  const { setSlot } = useContext(Context);
  return <div ref={setSlot} className="page-actions-slot" />;
}

export function PageAction({ children }: { children: ReactNode }) {
  const { slot } = useContext(Context);
  return slot ? createPortal(children, slot) : null;
}
