'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';
import { useSupervisor } from './supervisor-provider';
import { Fleet, useFleet } from './fleet';
import { ModemOverview } from './dashboard';

/** With one modem the home page is that modem; the fleet appears once there are two or more. */
export function Home() {
  usePreferences();
  const { snapshot, modems } = useSupervisor();
  const entries = useFleet();
  if (!snapshot && !modems.length) return <div className="card config-loading" role="status">{t("Loading…")}</div>;
  return entries.length >= 2 ? <Fleet /> : <ModemOverview />;
}
