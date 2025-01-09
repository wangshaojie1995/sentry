import * as Layout from 'sentry/components/layouts/thirds';
import useOrganization from 'sentry/utils/useOrganization';
import * as ModuleLayout from 'sentry/views/insights/common/components/moduleLayout';
import {ModulePageFilterBar} from 'sentry/views/insights/common/components/modulePageFilterBar';
import {ModulePageProviders} from 'sentry/views/insights/common/components/modulePageProviders';
import {ModulesOnboarding} from 'sentry/views/insights/common/components/modulesOnboarding';
import {ModuleBodyUpsellHook} from 'sentry/views/insights/common/components/moduleUpsellHookWrapper';
import {
  EAPNumberOfPipelinesChart,
  EAPPipelineDurationChart,
  EAPTotalTokensUsedChart,
  NumberOfPipelinesChart,
  PipelineDurationChart,
  TotalTokensUsedChart,
} from 'sentry/views/insights/llmMonitoring/components/charts/llmMonitoringCharts';
import {
  EAPPipelinesTable,
  PipelinesTable,
} from 'sentry/views/insights/llmMonitoring/components/tables/pipelinesTable';
import {AiHeader} from 'sentry/views/insights/pages/ai/aiPageHeader';
import {ModuleName} from 'sentry/views/insights/types';

export function LLMMonitoringPage() {
  const organization = useOrganization();

  const useEAP = organization.features.includes('insights-use-eap');

  return (
    <Layout.Page>
      <AiHeader module={ModuleName.AI} />
      <ModuleBodyUpsellHook moduleName={ModuleName.AI}>
        <Layout.Body>
          <Layout.Main fullWidth>
            <ModuleLayout.Layout>
              <ModuleLayout.Full>
                <ModulePageFilterBar moduleName={ModuleName.AI} />
              </ModuleLayout.Full>
              <ModulesOnboarding moduleName={ModuleName.AI}>
                <ModuleLayout.Third>
                  {useEAP ? <EAPTotalTokensUsedChart /> : <TotalTokensUsedChart />}
                </ModuleLayout.Third>
                <ModuleLayout.Third>
                  {useEAP ? <EAPNumberOfPipelinesChart /> : <NumberOfPipelinesChart />}
                </ModuleLayout.Third>
                <ModuleLayout.Third>
                  {useEAP ? <EAPPipelineDurationChart /> : <PipelineDurationChart />}
                </ModuleLayout.Third>
                <ModuleLayout.Full>
                  {useEAP ? <EAPPipelinesTable /> : <PipelinesTable />}
                </ModuleLayout.Full>
              </ModulesOnboarding>
            </ModuleLayout.Layout>
          </Layout.Main>
        </Layout.Body>
      </ModuleBodyUpsellHook>
    </Layout.Page>
  );
}

function PageWithProviders() {
  return (
    <ModulePageProviders moduleName="ai" analyticEventName="insight.page_loads.ai">
      <LLMMonitoringPage />
    </ModulePageProviders>
  );
}

export default PageWithProviders;
