import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import { SupervisorProvider } from '@/components/supervisor-provider';
import { AppShell } from '@/components/app-shell';
import { PreferencesProvider, preferenceBootstrap } from '@/components/preferences';
import './globals.css';

export const metadata: Metadata = {
  title: 'QMI Supervisor',
  description: 'Local modem control, connectivity and QMI telemetry.',
  robots: { index: false, follow: false },
};
export const viewport: Viewport = { width: 'device-width', initialScale: 1, themeColor: '#ffffff' };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="en" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{ __html: preferenceBootstrap }} /></head><body><PreferencesProvider><SupervisorProvider><AppShell>{children}</AppShell></SupervisorProvider></PreferencesProvider></body></html>;
}
