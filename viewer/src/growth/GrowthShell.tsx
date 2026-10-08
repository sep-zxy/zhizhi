import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { BookOpen, FolderGit2, Menu, Settings, X, House, Bell, MessagesSquare, GitBranch } from 'lucide-react';

import { useTranslation } from '../i18n/useTranslation';
import './GrowthShell.css';

function KnowledgeNavIcon({ size = 24 }: { size?: number }) { return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="1"/><path d="M3 8h18"/><path d="M7 12h7v4H7z"/></svg>; }

const NAV = [
  { view: 'overview', label: '今日归纳', Icon: House },
  { view: 'pending', label: '待确认', Icon: Bell },
  { view: 'cards', label: '知识卡', Icon: KnowledgeNavIcon },
  { view: 'wiki', label: '知识 Wiki', Icon: BookOpen },
  { view: 'discuss', label: '探讨', Icon: MessagesSquare },
  { view: 'projects', label: '项目与提交', Icon: GitBranch },
] as const;
const LEGACY_VIEWS: Record<string, string> = { today: 'overview', learning: 'pending', topics: 'discuss', review: 'cards' };

export default function GrowthShell({ children, projects, projectId, onProjectChange, latestScanAt, pendingCount }: {
  children: ReactNode; projects?: Array<{ project_id: string; name: string }>;
  latestScanAt?: string; pendingCount?: number; projectId?: string; onProjectChange?: (projectId: string) => void;
}) {
  const { t } = useTranslation();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [mobile, setMobile] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches);
  const menuButton = useRef<HTMLButtonElement>(null);
  const requested = new URLSearchParams(location.search).get('view') ?? 'overview';
  const activeView = LEGACY_VIEWS[requested] ?? requested;
  const currentLabel = location.pathname === '/settings' || activeView === 'settings'
    ? '设置' : NAV.find((item) => item.view === activeView)?.label ?? '知识卡';
  const projectQuery = projectId ? '&project=' + encodeURIComponent(projectId) : '';

  useEffect(() => { setMenuOpen(false); }, [location.pathname, location.search]);
  useEffect(() => {
    const query = window.matchMedia('(max-width: 768px)');
    const update = () => setMobile(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setMenuOpen(false);
        menuButton.current?.focus();
      }
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [menuOpen]);

  return <div className={`growth-shell${menuOpen ? ' growth-shell--open' : ''}`}>
    <a className="growth-shell__skip" href="#main-content" onClick={(event) => {
      event.preventDefault();
      document.getElementById('main-content')?.focus();
    }}>{t('A11y.skip_to_content')}</a>
    <aside className="growth-shell__sidebar" aria-label={t('Growth.nav_section')}
      aria-hidden={mobile && !menuOpen ? true : undefined}
      inert={mobile && !menuOpen ? true : undefined}>
      <div className="growth-shell__brand">
        <span className="growth-shell__mark" aria-hidden="true"><svg width="22" height="22" viewBox="0 0 128 128" fill="none" stroke="currentColor" strokeWidth="7" strokeLinecap="round" strokeLinejoin="round"><path d="M46 88V47M46 74H66C82 74 88 62 88 47"/><circle cx="46" cy="96" r="9"/><circle cx="46" cy="36" r="9"/><path d="M84 47C72 47 65 40 65 28C77 28 84 35 84 47ZM91 39C91 27 98 20 110 20C110 32 103 39 91 39Z" fill="currentColor" stroke="none"/></svg></span>
        <div><strong>知枝</strong></div>
      </div>
      <nav id="growth-navigation" aria-label={t('Growth.nav_section')}>
        {NAV.map(({ view, label, Icon }) => <Link key={view}
          to={`/growth?view=${view}${projectQuery}`}
          aria-current={location.pathname === '/growth' && activeView === view ? 'page' : undefined}
          className="growth-shell__nav-item">
          <Icon size={19} aria-hidden="true" /><span>{label}</span>{view==="pending"&&pendingCount!==undefined&&pendingCount>0&&<span className="growth-shell__nav-count" aria-label={pendingCount+" 个待确认候选"}>{pendingCount}</span>}
        </Link>)}
      </nav>
      <div className="growth-shell__footer">
        <Link to="/growth?view=settings" className="growth-shell__nav-item"
          aria-current={location.pathname === '/settings' || activeView === 'settings' ? 'page' : undefined}>
          <Settings size={19} aria-hidden="true" /><span>{t('Growth.view_settings')}</span>
        </Link>

      </div>
    </aside>
    <button type="button" className="growth-shell__backdrop"
      hidden={!mobile || !menuOpen} aria-label={t('A11y.close_menu')}
      onClick={() => { setMenuOpen(false); menuButton.current?.focus(); }} />
    <div className="growth-shell__main">
      <header className="growth-shell__topbar">
        <button type="button" className="growth-shell__menu" ref={menuButton}
          aria-label={menuOpen ? t('A11y.close_menu') : t('A11y.open_menu')}
          aria-controls="growth-navigation" aria-expanded={menuOpen}
          onClick={() => setMenuOpen((open) => !open)}>
          {menuOpen ? <X size={22} /> : <Menu size={22} />}
        </button>
        <span className="sr-only">{currentLabel}</span>
        {projects && onProjectChange && <label className="growth-shell__project-switcher">
          <FolderGit2 size={16} aria-hidden="true" />
          <span className="sr-only">工作区项目</span>
          <select aria-label="工作区项目" value={projectId ?? ''}
            onChange={(event) => onProjectChange(event.target.value)}>
            <option value="">全部项目</option>
            {projects.map((project) => <option key={project.project_id} value={project.project_id}>
              {project.name}</option>)}
          </select>
        </label>}
        <div className="growth-shell__top-actions">{latestScanAt&&<span className="growth-shell__latest-snapshot" title={new Date(latestScanAt).toLocaleString()}>最后扫描 {new Date(latestScanAt).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"})}</span>}
          <span className="growth-shell__avatar" aria-label="本机工作区">我</span>
        </div>
      </header>
      <main id="main-content" className="growth-shell__content" tabIndex={-1}
        inert={mobile && menuOpen ? true : undefined}>{children}</main>
    </div>
  </div>;
}
