import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';
import './captions.css';
import './live-library.css';
import './practice.css';

if (new URLSearchParams(location.search).has('captions')) document.documentElement.dataset.window = 'captions';

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
