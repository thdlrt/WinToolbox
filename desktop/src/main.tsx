import React from 'react';
import ReactDOM from 'react-dom/client';
const App = React.lazy(() => import('./App'));
const Orb = React.lazy(() => import('./Orb'));
const QuickMenu = React.lazy(() => import('./QuickMenu'));
const quick = new URLSearchParams(location.search).has('quick');
if (quick) document.documentElement.dataset.window = 'quick';
const orb = new URLSearchParams(location.search).has('orb');
if (orb) document.documentElement.dataset.window = 'orb';
import './styles.css';
import './captions.css';
import './live-library.css';
import './practice.css';

if (new URLSearchParams(location.search).has('captions')) document.documentElement.dataset.window = 'captions';

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><React.Suspense fallback={null}>{quick ? <QuickMenu /> : orb ? <Orb /> : <App />}</React.Suspense></React.StrictMode>);
