import { lazy, Suspense } from 'react';
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';
import ErrorBoundary from './components/ErrorBoundary';
import { useTranslation } from './i18n/useTranslation';

const GrowthPage = lazy(() => import('./pages/GrowthPage'));
const GrowthSettingsPage = lazy(() => import('./pages/GrowthSettingsPage'));

// Token-backed splash so the lazy boundary doesn't flash a white frame on
// slower networks.
function RouteFallback() {
  const { t } = useTranslation();
  return (
    <div
      className="route-fallback"
      role="status"
      aria-live="polite"
    >
      {t('Serve.loading')}
    </div>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <HashRouter>
        <Suspense fallback={<RouteFallback />}>
          <Routes>
            <Route path="/" element={<Navigate to="/growth?view=today" replace />} />
            <Route path="/growth" element={<ErrorBoundary><GrowthPage /></ErrorBoundary>} />
            <Route path="/settings" element={<ErrorBoundary><GrowthSettingsPage /></ErrorBoundary>} />
            <Route path="*" element={<Navigate to="/growth?view=today" replace />} />
          </Routes>
        </Suspense>
      </HashRouter>
    </ErrorBoundary>
  );
}
