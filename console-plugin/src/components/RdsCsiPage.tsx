import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Alert,
  Bullseye,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  Grid,
  GridItem,
  Label,
  PageSection,
  Progress,
  Spinner,
  Tab,
  Tabs,
  TabTitleText,
  Title,
  Tooltip,
} from '@patternfly/react-core';
import { SyncAltIcon } from '@patternfly/react-icons';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { fetchOverview } from '../api';
import type { NodeStatus, OverviewResponse, RdsStatus, VolumeStatus, WarningEvent } from '../types';
const POLL_MS = 15000;

// Keep spacing local to this plugin and aligned with PatternFly's medium gutter.
const SECTION_GAP = 'var(--pf-t--global--spacer--md, 1rem)';
const SectionStack = ({ children }: { children: ReactNode }) => (
  <div
    className="rds-csi-section-stack"
    style={{ display: 'flex', flexDirection: 'column', gap: SECTION_GAP, minWidth: 0 }}
  >
    {children}
  </div>
);

const formatBytes = (value?: number | null): string => {
  if (value === undefined || value === null || Number.isNaN(value)) return 'Unknown';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
};
const formatTime = (value?: string): string => {
  if (!value) return 'Unknown';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
};

const StatusLabel = ({ healthy, text }: { healthy?: boolean; text?: string }) => (
  <Label color={healthy ? 'green' : 'red'}>{text ?? (healthy ? 'Healthy' : 'Attention')}</Label>
);
const SummaryCard = ({ title, value, healthy }: { title: string; value: string; healthy?: boolean }) => (
  <Card isCompact>
    <CardHeader>
      <CardTitle>{title}</CardTitle>
    </CardHeader>
    <CardBody>
      <Flex alignItems={{ default: 'alignItemsCenter' }} justifyContent={{ default: 'justifyContentSpaceBetween' }}>
        <FlexItem>{value}</FlexItem>
        <FlexItem><StatusLabel healthy={healthy} /></FlexItem>
      </Flex>
    </CardBody>
  </Card>
);
const NodeTable = ({ nodes }: { nodes: NodeStatus[] }) => (
  <Table aria-label="MikroTik RDS CSI node connectivity">
    <Thead>
      <Tr>
        <Th>Node</Th>
        <Th>CSI pod</Th>
        <Th>Registered</Th>
        <Th>Route interface</Th>
        <Th>Source IP</Th>
        <Th>MTU</Th>
        <Th>Target</Th>
        <Th>NVMe</Th>
        <Th>Status</Th>
      </Tr>
    </Thead>
    <Tbody>
      {nodes.map((node) => (
        <Tr key={node.node}>
          <Td dataLabel="Node">
            <a href={`/k8s/cluster/nodes/${encodeURIComponent(node.node)}`}>{node.node}</a>
          </Td>
          <Td dataLabel="CSI pod">
            <Flex spaceItems={{ default: 'spaceItemsSm' }}>
              <FlexItem><Label color={node.csiPodReady ? 'green' : 'red'}>{node.csiPodReady ? 'Ready' : (node.csiPod?.phase || 'Not Ready')}</Label></FlexItem>
              {node.csiPod?.name && (
                <FlexItem><a href={`/k8s/ns/mikrotik-rds-csi/pods/${encodeURIComponent(node.csiPod.name)}`}>{node.csiPod.name}</a></FlexItem>
              )}
            </Flex>
          </Td>
          <Td dataLabel="Registered"><Label color={node.csiRegistered ? 'green' : 'red'}>{node.csiRegistered ? 'Yes' : 'No'}</Label></Td>
          <Td dataLabel="Route interface">{node.route.interface || 'Unknown'}</Td>
          <Td dataLabel="Source IP">
            <Flex spaceItems={{ default: 'spaceItemsSm' }}>
              <FlexItem>{node.route.sourceIp || 'Unknown'}</FlexItem>
              {node.route.sourceIp && !node.sourceIpUnique && <FlexItem><Label color="red">Duplicate</Label></FlexItem>}
            </Flex>
          </Td>
          <Td dataLabel="MTU">{node.interface.mtu ?? 'Unknown'}</Td>
          <Td dataLabel="Target">
            <Label color={node.targetReachable ? 'green' : 'red'}>
              {node.targetReachable ? 'Reachable' : 'Unreachable'}
            </Label>
          </Td>
          <Td dataLabel="NVMe">
            <Tooltip content={node.connectedNQNs.length ? node.connectedNQNs.join('\n') : 'No active RDS NQNs'}>
              <span>{node.activeControllers} controller{node.activeControllers === 1 ? '' : 's'}</span>
            </Tooltip>
          </Td>
          <Td dataLabel="Status"><StatusLabel healthy={node.healthy && !node.stale} /></Td>
        </Tr>
      ))}
      {nodes.length === 0 && (
        <Tr><Td colSpan={9}>No node status reports have been published yet.</Td></Tr>
      )}
    </Tbody>
  </Table>
);
const VolumeTable = ({ volumes }: { volumes: VolumeStatus[] }) => (
  <Table aria-label="MikroTik RDS CSI volumes">
    <Thead>
      <Tr>
        <Th>PVC</Th>
        <Th>PV</Th>
        <Th>Mode</Th>
        <Th>Size</Th>
        <Th>Handle</Th>
        <Th>NQN / target</Th>
        <Th>Consumer</Th>
        <Th>Phase</Th>
      </Tr>
    </Thead>
    <Tbody>
      {volumes.map((volume) => {
        const pvcHref = volume.namespace && volume.pvc
          ? `/k8s/ns/${encodeURIComponent(volume.namespace)}/persistentvolumeclaims/${encodeURIComponent(volume.pvc)}`
          : undefined;
        const pvHref = `/k8s/cluster/persistentvolumes/${encodeURIComponent(volume.pv)}`;
        const consumers = volume.consumers
          .map((consumer) => `${consumer.kind} ${consumer.namespace ? `${consumer.namespace}/` : ''}${consumer.name ?? ''}${consumer.node ? ` @ ${consumer.node}` : ''}`)
          .join(', ');
        return (
          <Tr key={volume.pv}>
            <Td dataLabel="PVC">{pvcHref ? <a href={pvcHref}>{volume.namespace}/{volume.pvc}</a> : 'Unbound'}</Td>
            <Td dataLabel="PV"><a href={pvHref}>{volume.pv}</a></Td>
            <Td dataLabel="Mode">{volume.mode || 'Filesystem'}</Td>
            <Td dataLabel="Size">{volume.capacity || formatBytes(volume.capacityBytes)}</Td>
            <Td dataLabel="Handle"><code>{volume.volumeHandle || '-'}</code></Td>
            <Td dataLabel="NQN / target">
              <div>{volume.nqn || '-'}</div>
              <div>{volume.targetAddress ? `${volume.targetAddress}:${volume.targetPort || 4420}` : '-'}</div>
            </Td>
            <Td dataLabel="Consumer">{consumers || 'None detected'}</Td>
            <Td dataLabel="Phase">{volume.phase || 'Unknown'}</Td>
          </Tr>
        );
      })}
      {volumes.length === 0 && <Tr><Td colSpan={8}>No RDS-backed persistent volumes found.</Td></Tr>}
    </Tbody>
  </Table>
);
const EventsTable = ({ events }: { events: WarningEvent[] }) => (
  <Table aria-label="Recent MikroTik RDS CSI warning events" variant="compact">
    <Thead>
      <Tr>
        <Th>Time</Th>
        <Th>Reason</Th>
        <Th>Object</Th>
        <Th>Message</Th>
        <Th>Count</Th>
      </Tr>
    </Thead>
    <Tbody>
      {events.map((event, index) => (
        <Tr key={`${event.time}-${event.name}-${index}`}>
          <Td dataLabel="Time">{formatTime(event.time)}</Td>
          <Td dataLabel="Reason">{event.reason || '-'}</Td>
          <Td dataLabel="Object">{[event.kind, event.namespace && `${event.namespace}/`, event.name].filter(Boolean).join(' ')}</Td>
          <Td dataLabel="Message">{event.message || '-'}</Td>
          <Td dataLabel="Count">{event.count ?? 1}</Td>
        </Tr>
      ))}
      {events.length === 0 && <Tr><Td colSpan={5}>No recent RDS/CSI warning events.</Td></Tr>}
    </Tbody>
  </Table>
);
const RdsDetails = ({ rds }: { rds: RdsStatus }) => {
  const pool = rds.pool;
  const usedPercent = pool && pool.totalBytes > 0
    ? Math.max(0, Math.min(100, ((pool.totalBytes - pool.freeBytes) / pool.totalBytes) * 100))
    : 0;
  return (
    <Grid hasGutter>
      <GridItem span={12} lg={6}>
        <Card>
          <CardHeader><CardTitle>RDS endpoint</CardTitle></CardHeader>
          <CardBody>
            <SectionStack>
              <DescriptionList isHorizontal>
                <DescriptionListGroup><DescriptionListTerm>REST</DescriptionListTerm><DescriptionListDescription><StatusLabel healthy={rds.reachable} text={rds.reachable ? 'Reachable' : 'Unreachable'} /></DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>RouterOS</DescriptionListTerm><DescriptionListDescription>{rds.routerOsVersion || 'Unknown'}</DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>Hardware</DescriptionListTerm><DescriptionListDescription>{rds.boardName || 'Unknown'}</DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>Uptime</DescriptionListTerm><DescriptionListDescription>{rds.uptime || 'Unknown'}</DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>NVMe/TCP target</DescriptionListTerm><DescriptionListDescription>{rds.target.address}:{rds.target.port}</DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>Managed volumes</DescriptionListTerm><DescriptionListDescription>{rds.managedVolumes ?? 'Unknown'}</DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>Active exports</DescriptionListTerm><DescriptionListDescription>{rds.activeExports ?? 'Unknown'}</DescriptionListDescription></DescriptionListGroup>
              </DescriptionList>
              {rds.error && <Alert isInline variant="danger" title="RDS status error">{rds.error}</Alert>}
            </SectionStack>
          </CardBody>
        </Card>
      </GridItem>
      <GridItem span={12} lg={6}>
        <Card>
          <CardHeader><CardTitle>Storage pool</CardTitle></CardHeader>
          <CardBody>
            {pool ? (
              <SectionStack>
                <DescriptionList isHorizontal>
                  <DescriptionListGroup><DescriptionListTerm>Slot</DescriptionListTerm><DescriptionListDescription>{pool.slot}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Path</DescriptionListTerm><DescriptionListDescription>{pool.path}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>State</DescriptionListTerm><DescriptionListDescription>{pool.state}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Mounted</DescriptionListTerm><DescriptionListDescription>{pool.mounted ? 'Yes' : 'No'}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Filesystem</DescriptionListTerm><DescriptionListDescription>{pool.filesystem}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Free</DescriptionListTerm><DescriptionListDescription>{formatBytes(pool.freeBytes)}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Usable after reserve</DescriptionListTerm><DescriptionListDescription>{formatBytes(pool.usableBytes)}</DescriptionListDescription></DescriptionListGroup>
                </DescriptionList>
                {pool.totalBytes > 0 && <Progress value={usedPercent} title="RDS pool used" />}
              </SectionStack>
            ) : <div>Pool status unavailable.</div>}
          </CardBody>
        </Card>
      </GridItem>
    </Grid>
  );
};
const OverviewTab = ({ data }: { data: OverviewResponse }) => {
  const c = data.cluster;
  const rdsFree = data.rds.pool ? formatBytes(data.rds.pool.usableBytes) : 'Unknown';
  const targetHealthy = data.nodes.items.length > 0 && data.nodes.items.every((node) => node.targetReachable && !node.stale);
  return (
    <SectionStack>
      {data.nodes.duplicateSourceIps.map((duplicate) => (
        <Alert key={duplicate.sourceIp} isInline variant="danger" title={`Duplicate storage source IP: ${duplicate.sourceIp}`}>
          Reported by {duplicate.nodes.join(', ')}. NVMe/TCP initiators must use unique source addresses.
        </Alert>
      ))}
      <Grid hasGutter>
        <GridItem span={12} sm={6} md={4} lg={2}><SummaryCard title="RDS REST" value={data.rds.reachable ? 'Reachable' : 'Unavailable'} healthy={data.rds.healthy} /></GridItem>
        <GridItem span={12} sm={6} md={4} lg={2}><SummaryCard title="CSI controller" value={`${c.controller.ready ?? 0}/${c.controller.desired ?? 0} Ready`} healthy={c.controller.healthy} /></GridItem>
        <GridItem span={12} sm={6} md={4} lg={2}><SummaryCard title="Node plugins" value={`${c.nodePlugin.ready ?? 0}/${c.nodePlugin.desired ?? 0} Ready`} healthy={c.nodePlugin.healthy} /></GridItem>
        <GridItem span={12} sm={6} md={4} lg={2}><SummaryCard title="CSI registration" value={`${c.csiRegistration.registered ?? 0}/${c.csiRegistration.totalNodes ?? 0} nodes`} healthy={c.csiRegistration.healthy} /></GridItem>
        <GridItem span={12} sm={6} md={4} lg={2}><SummaryCard title="NVMe target" value={`${data.nodes.items.filter((n) => n.targetReachable).length}/${data.nodes.items.length} reachable`} healthy={targetHealthy} /></GridItem>
        <GridItem span={12} sm={6} md={4} lg={2}><SummaryCard title="RDS capacity" value={`${rdsFree} usable`} healthy={data.rds.healthy} /></GridItem>
      </Grid>
      <Card>
        <CardHeader><CardTitle>Node connectivity</CardTitle></CardHeader>
        <CardBody><NodeTable nodes={data.nodes.items} /></CardBody>
      </Card>

      <Card>
        <CardHeader><CardTitle>Recent warnings</CardTitle></CardHeader>
        <CardBody><EventsTable events={data.events.items} /></CardBody>
      </Card>
    </SectionStack>
  );
};
const NodesTab = ({ nodes }: { nodes: NodeStatus[] }) => (
  <SectionStack>
    <NodeTable nodes={nodes} />
    {nodes.map((node) => (
      <Card key={`${node.node}-details`} isCompact>
        <CardHeader><CardTitle>{node.node}</CardTitle></CardHeader>
        <CardBody>
          <DescriptionList isHorizontal>
            <DescriptionListGroup><DescriptionListTerm>Last report</DescriptionListTerm><DescriptionListDescription>{formatTime(node.observedAt)}{node.stale ? ' (stale)' : ''}</DescriptionListDescription></DescriptionListGroup>
            <DescriptionListGroup><DescriptionListTerm>Route</DescriptionListTerm><DescriptionListDescription>{node.route.raw || 'Unknown'}</DescriptionListDescription></DescriptionListGroup>
            <DescriptionListGroup><DescriptionListTerm>Configured interface</DescriptionListTerm><DescriptionListDescription>{node.configuredInterface || 'Not enforced'}</DescriptionListDescription></DescriptionListGroup>
            <DescriptionListGroup><DescriptionListTerm>Operstate</DescriptionListTerm><DescriptionListDescription>{node.interface.operstate || 'Unknown'}</DescriptionListDescription></DescriptionListGroup>
            <DescriptionListGroup><DescriptionListTerm>Connected NQNs</DescriptionListTerm><DescriptionListDescription>{node.connectedNQNs.length ? node.connectedNQNs.join(', ') : 'None'}</DescriptionListDescription></DescriptionListGroup>
            <DescriptionListGroup><DescriptionListTerm>Last connectivity error</DescriptionListTerm><DescriptionListDescription>{node.lastConnectivityError ? `${node.lastConnectivityError}${node.lastConnectivityErrorAt ? ` (${formatTime(node.lastConnectivityErrorAt)})` : ''}` : 'None observed since reporter start'}</DescriptionListDescription></DescriptionListGroup>
            <DescriptionListGroup><DescriptionListTerm>Current errors</DescriptionListTerm><DescriptionListDescription>{node.errors.length ? node.errors.join('; ') : 'None'}</DescriptionListDescription></DescriptionListGroup>
          </DescriptionList>
        </CardBody>
      </Card>
    ))}
  </SectionStack>
);
const RdsCsiPage = () => {
  const [data, setData] = useState<OverviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<string | number>('overview');
  const refresh = useCallback(async (showSpinner = false) => {
    if (showSpinner) setLoading(true);
    try {
      const result = await fetchOverview();
      setData(result);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    refresh(true);
    const timer = window.setInterval(() => refresh(false), POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const observed = useMemo(() => data ? formatTime(data.observedAt) : 'Never', [data]);

  if (loading && !data) {
    return <PageSection><Bullseye><Spinner aria-label="Loading MikroTik RDS CSI status" /></Bullseye></PageSection>;
  }
  return (
    <SectionStack>
      <PageSection variant="secondary">
        <Flex justifyContent={{ default: 'justifyContentSpaceBetween' }} alignItems={{ default: 'alignItemsCenter' }}>
          <FlexItem>
            <Title headingLevel="h1">MikroTik RDS CSI</Title>
            <div>CSI, RDS pool, volume and NVMe/TCP connectivity status. Last refreshed: {observed}</div>
          </FlexItem>
          <FlexItem>
            <Button variant="secondary" icon={<SyncAltIcon />} onClick={() => refresh(true)}>Refresh</Button>
          </FlexItem>
        </Flex>
      </PageSection>
      <PageSection>
        <SectionStack>
          {error && <Alert isInline variant="danger" title="Unable to load RDS CSI status">{error}</Alert>}
          {data && !data.healthy && <Alert isInline variant="warning" title="One or more RDS CSI health checks need attention">Review node connectivity, RDS pool state, CSI readiness, and recent warnings below.</Alert>}
          {data && (
            <Tabs activeKey={activeTab} onSelect={(_event, key) => setActiveTab(key)} aria-label="MikroTik RDS CSI views">
              <Tab eventKey="overview" title={<TabTitleText>Overview</TabTitleText>}>
                <PageSection><OverviewTab data={data} /></PageSection>
              </Tab>
              <Tab eventKey="nodes" title={<TabTitleText>Nodes</TabTitleText>}>
                <PageSection><NodesTab nodes={data.nodes.items} /></PageSection>
              </Tab>
              <Tab eventKey="volumes" title={<TabTitleText>Volumes ({data.volumes.count})</TabTitleText>}>
                <PageSection><VolumeTable volumes={data.volumes.items} /></PageSection>
              </Tab>
              <Tab eventKey="rds" title={<TabTitleText>RDS</TabTitleText>}>
                <PageSection><RdsDetails rds={data.rds} /></PageSection>
              </Tab>
            </Tabs>
          )}
        </SectionStack>
      </PageSection>
    </SectionStack>
  );
};
export default RdsCsiPage;
