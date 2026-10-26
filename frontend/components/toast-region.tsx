'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { useEffect } from 'react';
import { Icon } from './icon';
import { useSupervisor } from './supervisor-provider';
import { notificationLifetime } from '@/lib/notifications';

/** The timer belongs to this notice ID, never to a status refresh or a route. */
export function ToastRegion() {
  usePreferences();
  const { notice, dismissNotice } = useSupervisor();
  const lifetime = notice ? notificationLifetime(notice.tone) : 0;
  const noticeId = notice?.id;
  useEffect(() => {
    if (noticeId === undefined) return;
    const timer = setTimeout(dismissNotice, lifetime);
    return () => clearTimeout(timer);
  }, [noticeId, lifetime, dismissNotice]);

  return <div className="toast-region" aria-label={t("Notifications")}>
    {notice && <div id="notification" className={'toast toast-' + notice.tone}
      role={notice.tone === 'error' ? 'alert' : 'status'} aria-atomic="true" data-notice-id={notice.id}>
      <span className="toast-icon"><Icon name={notice.tone === 'error' || notice.tone === 'warning' ? 'alert' : notice.tone === 'success' ? 'check' : 'info'} size={20} /></span>
      <span className="toast-message">{t(notice.message)}</span>
      <button type="button" className="toast-close icon-button" aria-label={t("Dismiss notification")} onClick={dismissNotice}><Icon name="close" size={17} /></button>
    </div>}
  </div>;
}
