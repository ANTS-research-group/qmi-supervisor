import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
  reactStrictMode: true,
  experimental: { agentUpgrade: 'security' },
  // The package build sets QMI_SUPERVISOR_VERSION; a plain dev build shows "dev".
  env: { NEXT_PUBLIC_BUILD_VERSION: process.env.QMI_SUPERVISOR_VERSION || 'dev' },
};

export default nextConfig;
