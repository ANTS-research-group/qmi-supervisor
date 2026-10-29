import type { CSSProperties } from 'react';

const paths: Record<string, string[]> = {
  sun: ['M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8', 'M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5'],
  moon: ['M21 12.8A9 9 0 0 1 11.2 3 9 9 0 1 0 21 12.8z'],
  dashboard: ['M3 3h7v7H3zM14 3h7v4h-7zM14 11h7v10h-7zM3 14h7v7H3z'],
  chart: ['M4 4v16h17', 'm7 14 4-5 4 3 5-8'],
  activity: ['M2 12h4l3-8 6 16 3-8h4'],
  settings: ['M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8', 'm9 3 1-2h4l1 2 2 1 2-.2 2 3-1 2v3l1 2-2 3-2-.2-2 1-1 2h-4l-1-2-2-1-2 .2-2-3 1-2V9L3 7l2-3 2 .2z'],
  signal: ['M4 17v3M9 12v8M14 7v13M19 3v17'],
  radio: ['M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6', 'M6 6a8.5 8.5 0 0 0 0 12M18 6a8.5 8.5 0 0 1 0 12M3 3a13 13 0 0 0 0 18M21 3a13 13 0 0 1 0 18'],
  router: ['M5 10h14a2 2 0 0 1 2 2v6H3v-6a2 2 0 0 1 2-2z', 'M7 10V3M17 10V3M7 14h.01M11 14h.01'],
  globe: ['M21 12a9 9 0 1 0-18 0 9 9 0 0 0 18 0', 'M3 12h18M12 3a17 17 0 0 0 0 18 17 17 0 0 0 0-18'],
  clock: ['M21 12a9 9 0 1 0-18 0 9 9 0 0 0 18 0', 'M12 7v5l3 2'],
  cpu: ['M7 7h10v10H7zM10 10h4v4h-4z', 'M9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4'],
  database: ['M20 5c0 2-4 3-8 3S4 7 4 5s4-3 8-3 8 1 8 3z', 'M4 5v14c0 2 4 3 8 3s8-1 8-3V5M4 12c0 2 4 3 8 3s8-1 8-3'],
  'arrow-up-right': ['M7 17 17 7M7 7h10v10'],
  'arrow-up': ['M12 20V4m-6 6 6-6 6 6'],
  'arrow-down': ['M12 4v16m-6-6 6 6 6-6'],
  'chevron-down': ['m6 9 6 6 6-6'],
  'chevron-right': ['m9 5 7 7-7 7'],
  check: ['m5 12 4 4L19 6'],
  alert: ['m12 3 10 18H2z', 'M12 9v4M12 17h.01'],
  info: ['M21 12a9 9 0 1 0-18 0 9 9 0 0 0 18 0', 'M12 11v6M12 7h.01'],
  refresh: ['M20 7a9 9 0 0 0-15-2L2 8M2 3v5h5', 'M4 17a9 9 0 0 0 15 2l3-3M22 21v-5h-5'],
  gps: ['M12 3v3M12 18v3M3 12h3M18 12h3', 'M18 12a6 6 0 1 0-12 0 6 6 0 0 0 12 0M14 12a2 2 0 1 0-4 0 2 2 0 0 0 4 0'],
  search: ['M17 10a7 7 0 1 0-14 0 7 7 0 0 0 14 0m-2 5 6 6'],
  download: ['M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4'],
  logout: ['M9 4H4v16h5M9 12h12m-5-5 5 5-5 5'],
  user: ['M16 7a4 4 0 1 0-8 0 4 4 0 0 0 8 0M4 21v-2a8 8 0 0 1 16 0v2'],
  lock: ['M6 10h12v11H6zM8 10V6a4 4 0 0 1 8 0v4'],
  menu: ['M4 6h16M4 12h16M4 18h16'],
  close: ['m6 6 12 12M6 18 18 6'],
  trash: ['M3 6h18M8 6V4h8v2', 'M6 6l1 14h10l1-14', 'M10 11v6M14 11v6'],
  shield: ['M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6z', 'm8 12 3 3 5-6'],
  plus: ['M12 5v14M5 12h14'],
  minus: ['M5 12h14'],
  'alert-circle': ['M21 12a9 9 0 1 0-18 0 9 9 0 0 0 18 0', 'M12 8v5M12 16.5h.01'],
  circle: ['M19 12a7 7 0 1 0-14 0 7 7 0 0 0 14 0'],
  modem: ['M5 7h14a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z', 'M7 11h2M7 13h2'],
  'chevrons-up-down': ['m7 9 5-5 5 5', 'm7 15 5 5 5-5'],
  grip: ['M8.25 6a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0M14.25 6a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0M8.25 12a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0M14.25 12a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0M8.25 18a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0M14.25 18a.75.75 0 1 0 1.5 0 .75.75 0 1 0-1.5 0'],
  sliders: ['M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3', 'M1 14h6M9 8h6M17 16h6'],
  eye: ['M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z', 'M15 12a3 3 0 1 0-6 0 3 3 0 0 0 6 0'],
  'eye-off': ['M10.6 5.1A9.7 9.7 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-2.4 3.3M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7a9.8 9.8 0 0 0 5.4-1.6', 'M9.9 9.9a3 3 0 0 0 4.2 4.2', 'm3 3 18 18'],
  logs: ['M5 3h14v18H5z', 'M8.5 8h7M8.5 12h7M8.5 16h4'],
  terminal: ['M3 4h18v16H3z', 'm7 9 3 3-3 3M13 15h4'],
};

export function Icon({ name, size = 20, className = '', style }: {name: string; size?: number; className?: string; style?: CSSProperties}) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className} style={style}>
    {(paths[name] || paths.info).map((path, index) => <path d={path} key={index} />)}
  </svg>;
}
