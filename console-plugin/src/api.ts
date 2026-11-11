import { consoleFetch } from '@openshift-console/dynamic-plugin-sdk';
import type { OverviewResponse } from './types';

const proxyBase = '/api/proxy/plugin/mikrotik-rds-csi-console/status';

export const fetchOverview = async (): Promise<OverviewResponse> => {
  const response = await consoleFetch(`${proxyBase}/api/v1/overview`);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.error) message = body.error;
    } catch {
      // Preserve the HTTP status when the backend returned a non-JSON error.
    }
    throw new Error(message);
  }
  return response.json();
};
