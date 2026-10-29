import { ErrorNote, Screen } from '@/components/ui';
import { PortfolioCard, usePortfolios } from '@/lib/portfolios';

export default function Portfolios() {
  const { portfolios, error, loading, reload } = usePortfolios();
  return (
    <Screen title="Portfolios" onRefresh={reload} refreshing={loading}>
      <ErrorNote error={error} />
      {portfolios.map((p, i) => <PortfolioCard key={p.id} p={p} index={i} wide />)}
    </Screen>
  );
}
