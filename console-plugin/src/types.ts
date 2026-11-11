export type Health = 'healthy' | 'warning' | 'danger' | 'unknown';

export interface ClusterComponent {
  ready?: number;
  desired?: number;
  registered?: number;
  totalNodes?: number;
  exists?: boolean;
  healthy: boolean;
  name?: string;
}

export interface RdsStatus {
  reachable: boolean;
  healthy: boolean;
  routerOsVersion?: string | null;
  boardName?: string | null;
  uptime?: string | null;
  pool?: {
    slot: string;
    path: string;
    state: string;
    mounted: boolean;
    filesystem: string;
    freeBytes: number;
    totalBytes: number;
    usableBytes: number;
    reserveBytes: number;
  } | null;
  managedVolumes?: number | null;
  activeExports?: number | null;
  target: { address: string; port: number };
  error?: string | null;
}

export interface NodeStatus {
  node: string;
  observedAt?: string;
  target: { address: string; port: number };
  route: { ok: boolean; interface?: string | null; sourceIp?: string | null; raw?: string; error?: string | null };
  interface: { exists: boolean; mtu?: number | null; operstate?: string | null; error?: string | null };
  configuredInterface?: string | null;
  interfaceMatchesConfiguration?: boolean;
  targetReachable: boolean;
  activeControllers: number;
  connectedNQNs: string[];
  healthy: boolean;
  errors: string[];
  csiPod?: { name?: string; ready?: boolean; phase?: string } | null;
  csiPodReady?: boolean;
  csiRegistered?: boolean;
  sourceIpUnique?: boolean;
  stale?: boolean;
  ageSeconds?: number | null;
  lastConnectivityError?: string | null;
  lastConnectivityErrorAt?: string | null;
}

export interface VolumeStatus {
  pv: string;
  namespace?: string;
  pvc?: string;
  mode?: string;
  capacity?: string;
  capacityBytes?: number | null;
  volumeHandle?: string;
  nqn?: string;
  targetAddress?: string;
  targetPort?: string | number;
  phase?: string;
  consumers: Array<{ kind: string; namespace?: string; name?: string; node?: string }>;
  reclaimPolicy?: string;
}

export interface WarningEvent {
  time: string;
  reason?: string;
  message?: string;
  count?: number;
  kind?: string;
  namespace?: string;
  name?: string;
  source?: string;
}

export interface OverviewResponse {
  apiVersion: string;
  observedAt: string;
  healthy: boolean;
  cluster: {
    controller: ClusterComponent;
    nodePlugin: ClusterComponent;
    csiRegistration: ClusterComponent;
    storageClass: ClusterComponent;
  };
  rds: RdsStatus;
  nodes: {
    items: NodeStatus[];
    duplicateSourceIps: Array<{ sourceIp: string; nodes: string[] }>;
    healthy: boolean;
  };
  volumes: { items: VolumeStatus[]; count: number };
  events: { items: WarningEvent[]; count: number };
}
