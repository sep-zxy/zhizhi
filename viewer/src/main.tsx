import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles/fonts.css';
import './growth/GrowthBase.css';
import './i18n/bootstrap';
import App from './App';
import { MantineProvider, createTheme } from '@mantine/core';
import '@mantine/core/styles.css';
const theme = createTheme({ primaryColor: 'pine', colors: { pine: ['#eff8f5','#deeee8','#bcddd2','#93cbb9','#6bbb9f','#49ae8e','#087e70','#09695f','#0b574f','#10483f'] }, fontFamily: 'Segoe UI, Growth Noto Sans SC, Microsoft YaHei, sans-serif', defaultRadius: 'md', components: { Badge: {defaultProps:{variant:'light',component:'span'}} } });

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <MantineProvider theme={theme} forceColorScheme="light"><App /></MantineProvider>
  </StrictMode>,
);
