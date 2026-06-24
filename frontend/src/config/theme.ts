const STORAGE_KEY = 'cc_theme';

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

export const THEMES = {
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
  light: {
    label: '浅色',
    colorScheme: 'light',
    tokens: {
      appBackground: '#f8fafc',
      foreground: '#111827',
      muted: '#374151',
      subtle: '#6b7280',
      chromeBackground: '#ffffff',
      chromeBorder: '#d1d5db',
      surfaceBackground: '#ffffff',
      surfaceRaised: '#f3f4f6',
      surfaceHover: '#e5e7eb',
      border: '#d1d5db',
      inputBackground: '#ffffff',
      inputBorder: '#cbd5e1',
      accentBackground: '#4f46e5',
      accentHover: '#4338ca',
      accentForeground: '#ffffff',
      accentMutedBackground: '#eef2ff',
      accentMutedForeground: '#4338ca',
      focusRing: '#6366f1',
      successForeground: '#15803d',
      warningForeground: '#a16207',
      dangerForeground: '#dc2626',
      infoForeground: '#2563eb',
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

  for (const [token, variable] of Object.entries(TOKEN_VARIABLES) as Array<[ThemeToken, `--ccm-${string}`]>) {
    root.style.setProperty(variable, definition.tokens[token]);
  }
}
