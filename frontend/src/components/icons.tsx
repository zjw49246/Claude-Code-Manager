/**
 * 中央图标模块（主题图标集的全站承载层）
 * ----------------------------------------------------------------------
 * 全站组件一律从本模块导入图标（禁止直接 import 'lucide-react'，
 * icons.test 有架构守卫断言；type-only 导入除外）。每个导出与 Lucide 同名、
 * 同 props 习惯（size/className/...），内部按当前主题的 iconSet 解析：
 *   feishu → IconPark（字节官方开源，Apache-2.0）outline 风，fill=currentColor 随文字色
 *   sf     → Ionicons 5（MIT），iOS 系统风，currentColor
 *   其余主题 / 无映射 → Lucide 原样回退（纯增强，绝不阻塞）
 * 新增图标：从 lucide-react 导入 + 补一行 themed(...) 映射（park/ion 可缺省）。
 * AppShell 导航图标走 config/iconSets.tsx 的语义注册表（two-tone 双色选中态），
 * 不经过本模块。本文件由映射表生成后人工维护。
 */
import type { ComponentType } from 'react';
import { useTheme } from '../hooks/useTheme';
import { getThemeOption } from '../config/theme';

import {
  Activity as L_Activity,
  AlertCircle as L_AlertCircle,
  Archive as L_Archive,
  ArchiveRestore as L_ArchiveRestore,
  ArrowDown as L_ArrowDown,
  ArrowLeft as L_ArrowLeft,
  ArrowUpCircle as L_ArrowUpCircle,
  Bot as L_Bot,
  Check as L_Check,
  CheckCircle2 as L_CheckCircle2,
  ChevronDown as L_ChevronDown,
  ChevronLeft as L_ChevronLeft,
  ChevronRight as L_ChevronRight,
  ChevronUp as L_ChevronUp,
  Circle as L_Circle,
  Clock as L_Clock,
  Copy as L_Copy,
  Download as L_Download,
  Edit2 as L_Edit2,
  Eraser as L_Eraser,
  Eye as L_Eye,
  EyeOff as L_EyeOff,
  FileKey as L_FileKey,
  FileText as L_FileText,
  Filter as L_Filter,
  Folder as L_Folder,
  FolderGit2 as L_FolderGit2,
  FolderOpen as L_FolderOpen,
  GitBranch as L_GitBranch,
  GitPullRequest as L_GitPullRequest,
  Globe as L_Globe,
  GripVertical as L_GripVertical,
  HardDrive as L_HardDrive,
  HelpCircle as L_HelpCircle,
  Image as L_Image,
  KeyRound as L_KeyRound,
  LayoutDashboard as L_LayoutDashboard,
  ListFilter as L_ListFilter,
  ListPlus as L_ListPlus,
  ListTodo as L_ListTodo,
  Loader as L_Loader,
  Loader2 as L_Loader2,
  LogOut as L_LogOut,
  Mail as L_Mail,
  MailOpen as L_MailOpen,
  Menu as L_Menu,
  MessageCircle as L_MessageCircle,
  MessageSquare as L_MessageSquare,
  MessagesSquare as L_MessagesSquare,
  Mic as L_Mic,
  MicOff as L_MicOff,
  MoreVertical as L_MoreVertical,
  Palette as L_Palette,
  PanelLeftClose as L_PanelLeftClose,
  PanelLeftOpen as L_PanelLeftOpen,
  Paperclip as L_Paperclip,
  Pencil as L_Pencil,
  Pin as L_Pin,
  Play as L_Play,
  Plus as L_Plus,
  Power as L_Power,
  RefreshCw as L_RefreshCw,
  RotateCcw as L_RotateCcw,
  Save as L_Save,
  ScrollText as L_ScrollText,
  Search as L_Search,
  Send as L_Send,
  Server as L_Server,
  Settings as L_Settings,
  Share2 as L_Share2,
  Shield as L_Shield,
  ShieldCheck as L_ShieldCheck,
  Sparkles as L_Sparkles,
  Square as L_Square,
  Star as L_Star,
  StopCircle as L_StopCircle,
  Syringe as L_Syringe,
  Tag as L_Tag,
  ToggleLeft as L_ToggleLeft,
  ToggleRight as L_ToggleRight,
  Trash2 as L_Trash2,
  Upload as L_Upload,
  User as L_User,
  UserPlus as L_UserPlus,
  Users as L_Users,
  Wifi as L_Wifi,
  WifiOff as L_WifiOff,
  Wrench as L_Wrench,
  X as L_X,
  XCircle as L_XCircle,
  Zap as L_Zap,
  ZapOff as L_ZapOff
} from 'lucide-react';
import {
  AddUser as P_AddUser,
  ArrowCircleUp as P_ArrowCircleUp,
  ArrowDown as P_ArrowDown,
  ArrowLeft as P_ArrowLeft,
  Attention as P_Attention,
  Box as P_Box,
  BranchTwo as P_BranchTwo,
  Check as P_Check,
  CheckOne as P_CheckOne,
  Close as P_Close,
  CloseOne as P_CloseOne,
  Comment as P_Comment,
  Comments as P_Comments,
  Copy as P_Copy,
  DashboardTwo as P_DashboardTwo,
  Delete as P_Delete,
  Down as P_Down,
  Download as P_Download,
  Drag as P_Drag,
  Edit as P_Edit,
  Erase as P_Erase,
  ExpandLeft as P_ExpandLeft,
  ExpandRight as P_ExpandRight,
  FileLock as P_FileLock,
  FileText as P_FileText,
  Filter as P_Filter,
  FolderClose as P_FolderClose,
  FolderCode as P_FolderCode,
  FolderOpen as P_FolderOpen,
  HamburgerButton as P_HamburgerButton,
  HardDisk as P_HardDisk,
  Help as P_Help,
  Injection as P_Injection,
  International as P_International,
  Key as P_Key,
  Left as P_Left,
  Lightning as P_Lightning,
  ListCheckbox as P_ListCheckbox,
  LoadingFour as P_LoadingFour,
  LoadingOne as P_LoadingOne,
  Log as P_Log,
  Logout as P_Logout,
  Magic as P_Magic,
  Mail as P_Mail,
  Message as P_Message,
  MoreOne as P_MoreOne,
  Paperclip as P_Paperclip,
  PauseOne as P_PauseOne,
  Pencil as P_Pencil,
  Peoples as P_Peoples,
  Pic as P_Pic,
  Pin as P_Pin,
  Platte as P_Platte,
  PlayOne as P_PlayOne,
  Plus as P_Plus,
  Power as P_Power,
  PreviewCloseOne as P_PreviewCloseOne,
  PreviewOpen as P_PreviewOpen,
  Protect as P_Protect,
  PullRequests as P_PullRequests,
  Refresh as P_Refresh,
  Right as P_Right,
  Round as P_Round,
  Save as P_Save,
  Search as P_Search,
  SendOne as P_SendOne,
  Server as P_Server,
  SettingTwo as P_SettingTwo,
  ShareTwo as P_ShareTwo,
  Shield as P_Shield,
  Square as P_Square,
  Star as P_Star,
  TagOne as P_TagOne,
  Time as P_Time,
  Tool as P_Tool,
  Undo as P_Undo,
  Up as P_Up,
  Upload as P_Upload,
  User as P_User,
  Voice as P_Voice,
  VoiceOff as P_VoiceOff,
  Wifi as P_Wifi
} from '@icon-park/react';
import {
  IoStarOutline,
  IoAdd,
  IoAlertCircle,
  IoArchive,
  IoArrowBack,
  IoArrowDown,
  IoArrowUndo,
  IoArrowUpCircle,
  IoAttach,
  IoBuild,
  IoChatbox,
  IoChatbubble,
  IoChatbubbles,
  IoCheckmark,
  IoCheckmarkCircle,
  IoChevronBack,
  IoChevronDown,
  IoChevronForward,
  IoChevronUp,
  IoClose,
  IoCloseCircle,
  IoCloudUpload,
  IoColorPalette,
  IoCopy,
  IoDocumentLock,
  IoDocumentText,
  IoDownload,
  IoEllipse,
  IoEllipsisVertical,
  IoEye,
  IoEyeOff,
  IoFilter,
  IoFlash,
  IoFlashOff,
  IoFolder,
  IoFolderOpen,
  IoGitBranch,
  IoGitPullRequest,
  IoGlobe,
  IoGrid,
  IoHelpCircle,
  IoImage,
  IoKey,
  IoList,
  IoLogOut,
  IoMail,
  IoMailOpen,
  IoMenu,
  IoMic,
  IoMicOff,
  IoPencil,
  IoPeople,
  IoPerson,
  IoPersonAdd,
  IoPin,
  IoPlay,
  IoPower,
  IoPricetag,
  IoPulse,
  IoRefresh,
  IoReload,
  IoReorderTwo,
  IoSave,
  IoSearch,
  IoSend,
  IoServer,
  IoSettingsSharp,
  IoShareSocial,
  IoShield,
  IoShieldCheckmark,
  IoSparkles,
  IoSquare,
  IoStar,
  IoStopCircle,
  IoTime,
  IoTrash,
  IoWifi
} from 'react-icons/io5';

export interface IconProps {
  size?: number | string;
  className?: string;
  [key: string]: unknown;
}

/* eslint-disable @typescript-eslint/no-explicit-any -- 三方图标库 props 类型互不兼容 */
function themed(
  Lucide: ComponentType<any>,
  Park?: ComponentType<any>,
  Ion?: ComponentType<any>,
  IonOutline?: ComponentType<any>,
): ComponentType<IconProps> {
  // 注意 fill 语义翻译：lucide 惯用 fill='currentColor'|'none' 表达实心/空心
  // （如收藏星标），但 IconPark 的 fill 是颜色数组、Ionicons 是 SVG 属性，
  // 直接透传会让图标隐形（2026-07-17 TaskForm 收藏按钮空白 bug）。
  function ThemedIcon({ size = 24, fill, ...rest }: IconProps) {
    const set = getThemeOption(useTheme()).iconSet;
    if (set === 'feishu' && Park) {
      const filledTheme = fill === 'currentColor' ? 'filled' : 'outline';
      return (
        <span data-icon-set="feishu" className="contents">
          <Park size={size} theme={filledTheme} fill="currentColor" strokeWidth={4} {...rest} />
        </span>
      );
    }
    if (set === 'sf' && Ion) {
      const C = fill === 'none' && IonOutline ? IonOutline : Ion;
      return (
        <span data-icon-set="sf" className="contents">
          <C size={size} {...rest} />
        </span>
      );
    }
    return <Lucide size={size} fill={fill as string | undefined} {...rest} />;
  }
  return ThemedIcon;
}

export const Activity = themed(L_Activity, undefined, IoPulse);
export const AlertCircle = themed(L_AlertCircle, P_Attention, IoAlertCircle);
export const Archive = themed(L_Archive, P_Box, IoArchive);
export const ArchiveRestore = L_ArchiveRestore; // 无合适映射，保持 Lucide
export const ArrowDown = themed(L_ArrowDown, P_ArrowDown, IoArrowDown);
export const ArrowLeft = themed(L_ArrowLeft, P_ArrowLeft, IoArrowBack);
export const ArrowUpCircle = themed(L_ArrowUpCircle, P_ArrowCircleUp, IoArrowUpCircle);
export const Bot = L_Bot; // 无合适映射，保持 Lucide
export const Check = themed(L_Check, P_Check, IoCheckmark);
export const CheckCircle2 = themed(L_CheckCircle2, P_CheckOne, IoCheckmarkCircle);
export const ChevronDown = themed(L_ChevronDown, P_Down, IoChevronDown);
export const ChevronLeft = themed(L_ChevronLeft, P_Left, IoChevronBack);
export const ChevronRight = themed(L_ChevronRight, P_Right, IoChevronForward);
export const ChevronUp = themed(L_ChevronUp, P_Up, IoChevronUp);
export const Circle = themed(L_Circle, P_Round, IoEllipse);
export const Clock = themed(L_Clock, P_Time, IoTime);
export const Copy = themed(L_Copy, P_Copy, IoCopy);
export const Download = themed(L_Download, P_Download, IoDownload);
export const Edit2 = themed(L_Edit2, P_Edit, IoPencil);
export const Eraser = themed(L_Eraser, P_Erase, undefined);
export const Eye = themed(L_Eye, P_PreviewOpen, IoEye);
export const EyeOff = themed(L_EyeOff, P_PreviewCloseOne, IoEyeOff);
export const FileKey = themed(L_FileKey, P_FileLock, IoDocumentLock);
export const FileText = themed(L_FileText, P_FileText, IoDocumentText);
export const Filter = themed(L_Filter, P_Filter, IoFilter);
export const Folder = themed(L_Folder, P_FolderClose, IoFolder);
export const FolderGit2 = themed(L_FolderGit2, P_FolderCode, IoFolder);
export const FolderOpen = themed(L_FolderOpen, P_FolderOpen, IoFolderOpen);
export const GitBranch = themed(L_GitBranch, P_BranchTwo, IoGitBranch);
export const GitPullRequest = themed(L_GitPullRequest, P_PullRequests, IoGitPullRequest);
export const Globe = themed(L_Globe, P_International, IoGlobe);
export const GripVertical = themed(L_GripVertical, P_Drag, IoReorderTwo);
export const HardDrive = themed(L_HardDrive, P_HardDisk, undefined);
export const HelpCircle = themed(L_HelpCircle, P_Help, IoHelpCircle);
export const Image = themed(L_Image, P_Pic, IoImage);
export const KeyRound = themed(L_KeyRound, P_Key, IoKey);
export const LayoutDashboard = themed(L_LayoutDashboard, P_DashboardTwo, IoGrid);
export const ListFilter = themed(L_ListFilter, P_Filter, IoFilter);
export const ListPlus = L_ListPlus; // 无合适映射，保持 Lucide
export const ListTodo = themed(L_ListTodo, P_ListCheckbox, IoList);
export const Loader = themed(L_Loader, P_LoadingOne, IoReload);
export const Loader2 = themed(L_Loader2, P_LoadingFour, IoReload);
export const LogOut = themed(L_LogOut, P_Logout, IoLogOut);
export const Mail = themed(L_Mail, P_Mail, IoMail);
export const MailOpen = themed(L_MailOpen, undefined, IoMailOpen);
export const Menu = themed(L_Menu, P_HamburgerButton, IoMenu);
export const MessageCircle = themed(L_MessageCircle, P_Message, IoChatbubble);
export const MessageSquare = themed(L_MessageSquare, P_Comment, IoChatbox);
export const MessagesSquare = themed(L_MessagesSquare, P_Comments, IoChatbubbles);
export const Mic = themed(L_Mic, P_Voice, IoMic);
export const MicOff = themed(L_MicOff, P_VoiceOff, IoMicOff);
export const MoreVertical = themed(L_MoreVertical, P_MoreOne, IoEllipsisVertical);
export const Palette = themed(L_Palette, P_Platte, IoColorPalette);
export const PanelLeftClose = themed(L_PanelLeftClose, P_ExpandRight, undefined);
export const PanelLeftOpen = themed(L_PanelLeftOpen, P_ExpandLeft, undefined);
export const Paperclip = themed(L_Paperclip, P_Paperclip, IoAttach);
export const Pencil = themed(L_Pencil, P_Pencil, IoPencil);
export const Pin = themed(L_Pin, P_Pin, IoPin);
export const Play = themed(L_Play, P_PlayOne, IoPlay);
export const Plus = themed(L_Plus, P_Plus, IoAdd);
export const Power = themed(L_Power, P_Power, IoPower);
export const RefreshCw = themed(L_RefreshCw, P_Refresh, IoRefresh);
export const RotateCcw = themed(L_RotateCcw, P_Undo, IoArrowUndo);
export const Save = themed(L_Save, P_Save, IoSave);
export const ScrollText = themed(L_ScrollText, P_Log, IoDocumentText);
export const Search = themed(L_Search, P_Search, IoSearch);
export const Send = themed(L_Send, P_SendOne, IoSend);
export const Server = themed(L_Server, P_Server, IoServer);
export const Settings = themed(L_Settings, P_SettingTwo, IoSettingsSharp);
export const Share2 = themed(L_Share2, P_ShareTwo, IoShareSocial);
export const Shield = themed(L_Shield, P_Shield, IoShield);
export const ShieldCheck = themed(L_ShieldCheck, P_Protect, IoShieldCheckmark);
export const Sparkles = themed(L_Sparkles, P_Magic, IoSparkles);
export const Square = themed(L_Square, P_Square, IoSquare);
export const Star = themed(L_Star, P_Star, IoStar, IoStarOutline);
export const StopCircle = themed(L_StopCircle, P_PauseOne, IoStopCircle);
export const Syringe = themed(L_Syringe, P_Injection, undefined);
export const Tag = themed(L_Tag, P_TagOne, IoPricetag);
export const ToggleLeft = L_ToggleLeft; // 无合适映射，保持 Lucide
export const ToggleRight = L_ToggleRight; // 无合适映射，保持 Lucide
export const Trash2 = themed(L_Trash2, P_Delete, IoTrash);
export const Upload = themed(L_Upload, P_Upload, IoCloudUpload);
export const User = themed(L_User, P_User, IoPerson);
export const UserPlus = themed(L_UserPlus, P_AddUser, IoPersonAdd);
export const Users = themed(L_Users, P_Peoples, IoPeople);
export const Wifi = themed(L_Wifi, P_Wifi, IoWifi);
export const WifiOff = L_WifiOff; // 无合适映射，保持 Lucide
export const Wrench = themed(L_Wrench, P_Tool, IoBuild);
export const X = themed(L_X, P_Close, IoClose);
export const XCircle = themed(L_XCircle, P_CloseOne, IoCloseCircle);
export const Zap = themed(L_Zap, P_Lightning, IoFlash);
export const ZapOff = themed(L_ZapOff, undefined, IoFlashOff);
