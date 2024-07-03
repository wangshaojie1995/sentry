import styled from '@emotion/styled';

import {DataSection} from 'sentry/components/events/styles';
import GlobalAppStoreConnectUpdateAlert from 'sentry/components/globalAppStoreConnectUpdateAlert';
import {space} from 'sentry/styles/space';
import type {Event} from 'sentry/types/event';
import type {Group} from 'sentry/types/group';
import type {Project} from 'sentry/types/project';
import useOrganization from 'sentry/utils/useOrganization';
import {GroupEventCarousel} from 'sentry/views/issueDetails/groupEventCarousel';
import {TraceLink} from 'sentry/views/issueDetails/traceTimeline/traceLink';
import {TraceTimeline} from 'sentry/views/issueDetails/traceTimeline/traceTimeline';
import {useTraceTimelineEvents} from 'sentry/views/issueDetails/traceTimeline/useTraceTimelineEvents';

type GroupEventHeaderProps = {
  event: Event;
  group: Group;
  project: Project;
};

function GroupEventHeader({event, group, project}: GroupEventHeaderProps) {
  const organization = useOrganization();
  // This is also called within the TraceTimeline component but caching will save a second call
  const {isLoading, oneOtherIssueEvent} = useTraceTimelineEvents({
    event,
  });

  return (
    <StyledDataSection>
      <GroupEventCarousel group={group} event={event} projectSlug={project.slug} />
      {!isLoading && (
        <StyledTraceLink>
          {/* This message is used when showing a trace-related issue */}
          {oneOtherIssueEvent && <span>One other issue appears in the same trace.</span>}
          <TraceLink event={event} />
        </StyledTraceLink>
      )}
      {/* This renders either the trace timeline or the trace-related issue */}
      <TraceTimeline event={event} />
      <StyledGlobalAppStoreConnectUpdateAlert
        project={project}
        organization={organization}
      />
    </StyledDataSection>
  );
}

const StyledGlobalAppStoreConnectUpdateAlert = styled(GlobalAppStoreConnectUpdateAlert)`
  margin: ${space(0.5)} 0;
`;

const StyledDataSection = styled(DataSection)`
  padding: ${space(1)} ${space(2)} 0;

  @media (min-width: ${p => p.theme.breakpoints.medium}) {
    padding: ${space(1.5)} ${space(4)} 0;
  }
`;

const StyledTraceLink = styled('div')`
  display: flex;
  white-space: nowrap;
  overflow: hidden;
  gap: ${space(0.25)};
`;

export default GroupEventHeader;
