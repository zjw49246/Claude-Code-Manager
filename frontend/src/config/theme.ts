const STORAGE_KEY = 'cc_theme';

// Follow-up (semantic-theme migration, from PR #29 review):
//  - Task status dots in TaskList (`statusColors[t.status] || 'bg-gray-500'`) and
//    provider/shared badges (green=Codex, blue=Claude, orange=Shared) still use raw
//    Tailwind accent classes. They currently follow the theme only via the
//    --color-gray-*/accent compat overrides in index.css. Fold them into the
//    semantic token set (success/danger/warning/info) once those roles cover the
//    status palette, then the per-theme compat blocks can start shrinking.

type ThemeToken =
  | 'appBackground'
  | 'foreground'
  | 'muted'
  | 'subtle'
  | 'chromeBackground'
  | 'chromeBorder'
  | 'surfaceBackground'
  | 'surfaceRaised'
  | 'surfaceHover'
  | 'border'
  | 'inputBackground'
  | 'inputBorder'
  | 'accentBackground'
  | 'accentHover'
  | 'accentForeground'
  | 'accentMutedBackground'
  | 'accentMutedForeground'
  | 'focusRing'
  | 'successForeground'
  | 'warningForeground'
  | 'dangerForeground'
  | 'infoForeground';

type ThemeDefinition = {
  label: string;
  colorScheme: 'dark' | 'light';
  tokens: Record<ThemeToken, string>;
};

const TOKEN_VARIABLES: Record<ThemeToken, `--ccm-${string}`> = {
  appBackground: '--ccm-app-background',
  foreground: '--ccm-foreground',
  muted: '--ccm-muted',
  subtle: '--ccm-subtle',
  chromeBackground: '--ccm-chrome-background',
  chromeBorder: '--ccm-chrome-border',
  surfaceBackground: '--ccm-surface-background',
  surfaceRaised: '--ccm-surface-raised',
  surfaceHover: '--ccm-surface-hover',
  border: '--ccm-border',
  inputBackground: '--ccm-input-background',
  inputBorder: '--ccm-input-border',
  accentBackground: '--ccm-accent-background',
  accentHover: '--ccm-accent-hover',
  accentForeground: '--ccm-accent-foreground',
  accentMutedBackground: '--ccm-accent-muted-background',
  accentMutedForeground: '--ccm-accent-muted-foreground',
  focusRing: '--ccm-focus-ring',
  successForeground: '--ccm-success-foreground',
  warningForeground: '--ccm-warning-foreground',
  dangerForeground: '--ccm-danger-foreground',
  infoForeground: '--ccm-info-foreground',
};

// Cached at module load — TOKEN_VARIABLES is const, so this never changes.
const TOKEN_ENTRIES = Object.entries(TOKEN_VARIABLES) as Array<[ThemeToken, `--ccm-${string}`]>;

export const THEMES = {
  // NOTE: The dark theme's token values are duplicated in index.css `:root`, which
  // acts as the pre-JS fallback. Keep the two in sync when editing dark values.
  dark: {
    label: '深色',
    colorScheme: 'dark',
    tokens: {
      appBackground: '#030712',
      foreground: '#f9fafb',
      muted: '#d1d5db',
      subtle: '#9ca3af',
      chromeBackground: '#111827',
      chromeBorder: '#374151',
      surfaceBackground: '#1f2937',
      surfaceRaised: '#111827',
      surfaceHover: '#374151',
      border: '#374151',
      inputBackground: '#374151',
      inputBorder: '#4b5563',
      accentBackground: '#4f46e5',
      accentHover: '#6366f1',
      accentForeground: '#ffffff',
      accentMutedBackground: '#312e81',
      accentMutedForeground: '#c7d2fe',
      focusRing: '#6366f1',
      successForeground: '#4ade80',
      warningForeground: '#facc15',
      dangerForeground: '#f87171',
      infoForeground: '#60a5fa',
    },
  },
  ocean: {
    label: '海蓝',
    colorScheme: 'dark',
    tokens: {
      appBackground: '#06131f',
      foreground: '#e8f6ff',
      muted: '#bed0d9',
      subtle: '#91aebd',
      chromeBackground: '#0b1f30',
      chromeBorder: '#1c4562',
      surfaceBackground: '#123047',
      surfaceRaised: '#0b1f30',
      surfaceHover: '#1c4562',
      border: '#1c4562',
      inputBackground: '#123047',
      inputBorder: '#2f5f7c',
      accentBackground: '#0891b2',
      accentHover: '#06b6d4',
      accentForeground: '#ecfeff',
      accentMutedBackground: '#164e63',
      accentMutedForeground: '#a5f3fc',
      focusRing: '#22d3ee',
      successForeground: '#86efac',
      warningForeground: '#fcd34d',
      dangerForeground: '#fda4af',
      infoForeground: '#7dd3fc',
    },
  },
  forest: {
    label: '森林',
    colorScheme: 'dark',
    tokens: {
      appBackground: '#07130d',
      foreground: '#f0f8ef',
      muted: '#c2cdbc',
      subtle: '#9aab98',
      chromeBackground: '#102015',
      chromeBorder: '#29472f',
      surfaceBackground: '#1a3221',
      surfaceRaised: '#102015',
      surfaceHover: '#29472f',
      border: '#29472f',
      inputBackground: '#1a3221',
      inputBorder: '#3f6046',
      accentBackground: '#15803d',
      accentHover: '#16a34a',
      accentForeground: '#f0fdf4',
      accentMutedBackground: '#14532d',
      accentMutedForeground: '#bbf7d0',
      focusRing: '#4ade80',
      successForeground: '#86efac',
      warningForeground: '#fde047',
      dangerForeground: '#fca5a5',
      infoForeground: '#93c5fd',
    },
  },
  rose: {
    label: '莓红',
    colorScheme: 'dark',
    tokens: {
      appBackground: '#1a0b12',
      foreground: '#fff1f5',
      muted: '#dcc0c9',
      subtle: '#bd95a4',
      chromeBackground: '#2a121d',
      chromeBorder: '#55283c',
      surfaceBackground: '#3d1c2b',
      surfaceRaised: '#2a121d',
      surfaceHover: '#55283c',
      border: '#55283c',
      inputBackground: '#3d1c2b',
      inputBorder: '#71384f',
      accentBackground: '#db2777',
      accentHover: '#ec4899',
      accentForeground: '#fff1f5',
      accentMutedBackground: '#831843',
      accentMutedForeground: '#fbcfe8',
      focusRing: '#f472b6',
      successForeground: '#6ee7b7',
      warningForeground: '#fcd34d',
      dangerForeground: '#fda4af',
      infoForeground: '#c4b5fd',
    },
  },
  onedark: {
    label: 'One Dark',
    colorScheme: 'dark',
    tokens: {
      appBackground: '#21252b',
      foreground: '#d7dae0',
      muted: '#abb2bf',
      subtle: '#7f848e',
      chromeBackground: '#21252b',
      chromeBorder: '#181a1f',
      surfaceBackground: '#282c34',
      surfaceRaised: '#21252b',
      surfaceHover: '#2c313c',
      border: '#3e4451',
      inputBackground: '#1d2026',
      inputBorder: '#3e4451',
      accentBackground: '#4577e6',
      accentHover: '#528bff',
      accentForeground: '#ffffff',
      accentMutedBackground: '#2b3a54',
      accentMutedForeground: '#9cc0f5',
      focusRing: '#61afef',
      successForeground: '#98c379',
      warningForeground: '#e5c07b',
      dangerForeground: '#e06c75',
      infoForeground: '#61afef',
    },
  },
  dracula: {
    label: 'Dracula',
    colorScheme: 'dark',
    tokens: {
      appBackground: '#21222c',
      foreground: '#f8f8f2',
      muted: '#d4d6e4',
      subtle: '#6272a4',
      chromeBackground: '#282a36',
      chromeBorder: '#191a21',
      surfaceBackground: '#343645',
      surfaceRaised: '#282a36',
      surfaceHover: '#44475a',
      border: '#44475a',
      inputBackground: '#21222c',
      inputBorder: '#44475a',
      accentBackground: '#7c5cc7',
      accentHover: '#9070d8',
      accentForeground: '#ffffff',
      accentMutedBackground: '#3b2f5e',
      accentMutedForeground: '#d6bcfd',
      focusRing: '#bd93f9',
      successForeground: '#50fa7b',
      warningForeground: '#f1fa8c',
      dangerForeground: '#ff5555',
      infoForeground: '#8be9fd',
    },
  },
  nord: {
    label: 'Nord',
    colorScheme: 'dark',
    tokens: {
      appBackground: '#242933',
      foreground: '#eceff4',
      muted: '#d8dee9',
      subtle: '#7b88a1',
      chromeBackground: '#2e3440',
      chromeBorder: '#3b4252',
      surfaceBackground: '#3b4252',
      surfaceRaised: '#2e3440',
      surfaceHover: '#434c5e',
      border: '#434c5e',
      inputBackground: '#2e3440',
      inputBorder: '#4c566a',
      accentBackground: '#5e81ac',
      accentHover: '#81a1c1',
      accentForeground: '#eceff4',
      accentMutedBackground: '#3b4a5e',
      accentMutedForeground: '#a3c1e0',
      focusRing: '#88c0d0',
      successForeground: '#a3be8c',
      warningForeground: '#ebcb8b',
      dangerForeground: '#bf616a',
      infoForeground: '#88c0d0',
    },
  },
  tokyonight: {
    label: 'Tokyo Night',
    colorScheme: 'dark',
    tokens: {
      appBackground: '#16161e',
      foreground: '#c0caf5',
      muted: '#a9b1d6',
      subtle: '#565f89',
      chromeBackground: '#1a1b26',
      chromeBorder: '#292e42',
      surfaceBackground: '#1f2335',
      surfaceRaised: '#1a1b26',
      surfaceHover: '#292e42',
      border: '#292e42',
      inputBackground: '#1a1b26',
      inputBorder: '#3b4261',
      accentBackground: '#4d62a6',
      accentHover: '#7aa2f7',
      accentForeground: '#ffffff',
      accentMutedBackground: '#2a3158',
      accentMutedForeground: '#a9c0ff',
      focusRing: '#7aa2f7',
      successForeground: '#9ece6a',
      warningForeground: '#e0af68',
      dangerForeground: '#f7768e',
      infoForeground: '#7dcfff',
    },
  },
} as const satisfies Record<string, ThemeDefinition>;

export type Theme = keyof typeof THEMES;

export const THEME_OPTIONS = Object.entries(THEMES).map(([value, theme]) => ({
  value: value as Theme,
  label: theme.label,
}));

const THEME_VALUES = new Set<Theme>(Object.keys(THEMES) as Theme[]);

export function getTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY) as Theme | null;
  return stored && THEME_VALUES.has(stored) ? stored : 'dark';
}

export function setTheme(theme: Theme) {
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

export function applyTheme(theme?: Theme) {
  const t = theme || getTheme();
  const definition = THEMES[t];
  const root = document.documentElement;

  root.classList.remove('light');
  root.dataset.theme = t;
  root.style.colorScheme = definition.colorScheme;

  for (const [token, variable] of TOKEN_ENTRIES) {
    root.style.setProperty(variable, definition.tokens[token]);
  }
}
