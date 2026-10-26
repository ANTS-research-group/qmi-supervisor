import { notFound } from 'next/navigation';
import { LegacyMetricsRedirect, Metrics } from '@/components/metrics';
const tabs = ['charts', 'data', 'signal', 'performance'] as const;
export function generateStaticParams() { return tabs.map(tab => ({ tab })); }
export const dynamicParams = false;
export default async function MetricsPage({ params }: {params: Promise<{tab: string}>}) {
  const { tab } = await params;
  if (tab === 'signal' || tab === 'performance') return <LegacyMetricsRedirect view={tab} />;
  if (tab !== 'charts' && tab !== 'data') notFound();
  return <Metrics tab={tab} />;
}
