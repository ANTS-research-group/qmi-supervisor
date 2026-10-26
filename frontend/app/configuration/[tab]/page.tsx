import { notFound } from 'next/navigation';
import { Configuration, LegacyTelemetryRedirect } from '@/components/configuration';
const tabs = ['modem', 'telemetry', 'web'] as const;
export function generateStaticParams() { return tabs.map(tab => ({ tab })); }
export const dynamicParams = false;
export default async function ConfigurationPage({ params }: {params: Promise<{tab: string}>}) {
  const { tab } = await params;
  if (!tabs.includes(tab as typeof tabs[number])) notFound();
  if (tab === 'telemetry') return <LegacyTelemetryRedirect />;
  return <Configuration scope={tab === 'web' ? 'web' : 'profile'} />;
}
