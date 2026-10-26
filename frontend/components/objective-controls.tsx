'use client';

import Link from 'next/link';
import { t } from '@/lib/messages';
import { usePreferences } from './preferences';
import { useSupervisor } from './supervisor-provider';
import { Icon } from './icon';
import { Switch } from './form-controls';
import { requestConfigSection } from './configuration';
import { modemHref, statePresentation } from '@/lib/fleet';

export function ObjectiveControls() {
  usePreferences();
  const { config, snapshot, editable, saving, objectiveBusy, setObjective, selectedModem } = useSupervisor();
  return <section className="objective-controls" aria-label={t('Objectives')}>
    <div className="objective-grid">{(['connection', 'telemetry'] as const).map(name => {
      const enabled = name === 'connection' ? config?.modem?.mode === 'connect' : config?.telemetry?.enabled === true;
      const feature = snapshot?.features?.[name];
      const pending = objectiveBusy === selectedModem + ':' + name || feature?.pending === true || (feature && feature.enabled !== enabled);
      const state = pending ? t(enabled ? 'Pending' : 'Stopping') : !enabled ? t('Disabled') : feature?.fresh === false ? t('Unconfirmed state')
        : feature?.state ? statePresentation(feature.state).label : name === 'connection' && snapshot?.state ? statePresentation(snapshot.state).label : t('Starting');
      const title = name === 'connection' ? 'Autonomous connection' : 'Monitoring';
      const section = name === 'connection' ? 'connection' : 'capture';
      return <div className="card objective-card" key={name}>
        <div className="objective-main"><Icon name={name === 'connection' ? 'signal' : 'activity'} size={23} />
          <label htmlFor={'objective-' + name}>{t(title)}</label>
          <Switch id={'objective-' + name} checked={enabled} disabled={!config || !editable || saving || objectiveBusy !== null}
            aria-describedby={'objective-state-' + name} onChange={event => void setObjective(name, event.currentTarget.checked)} /></div>
        <div className="objective-bottom"><span id={'objective-state-' + name} role="status">{state}</span>
          <Link href={modemHref('/configuration/modem/', selectedModem, { section })} prefetch={false}
            onClick={() => requestConfigSection('profile', section)}>{t('Configure')} <Icon name="arrow-up-right" size={14} /></Link></div>
        {(enabled || pending || feature?.effective) && feature?.error && <p className="objective-error">{t(String(feature.error))}</p>}
        {(enabled || pending) && feature?.retry_in_seconds > 0 && <p className="muted small">{t('Retry in {seconds} s', { seconds: Math.ceil(feature.retry_in_seconds) })}</p>}
      </div>;
    })}</div>
  </section>;
}
