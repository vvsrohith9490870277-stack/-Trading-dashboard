import React,{lazy,Suspense} from 'react';import {createRoot} from 'react-dom/client';import {isPortalPath} from './portal/router';import './style.css';
// Path routes (/paper, /strategy-tester, /strategy-comparison, /performance, /stock/:symbol) open the Atlas Desk portal; "/" keeps the classic hash-routed dashboard.
const Root=isPortalPath(window.location.pathname)?lazy(()=>import('./portal/Portal')):lazy(()=>import('./App'));
createRoot(document.getElementById('root')).render(<React.StrictMode><Suspense fallback={null}><Root/></Suspense></React.StrictMode>);
