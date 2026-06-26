import { useState, useEffect } from 'react';
import { Plus, Pencil, Trash2, X, Check } from 'lucide-react';
import { api } from '../api/client';

interface UserSkill {
  id: number;
  name: string;
  description: string;
  content: string;
  created_at: string | null;
  updated_at: string | null;
}

export function SkillsPage() {
  const [skills, setSkills] = useState<UserSkill[]>([]);
  const [editing, setEditing] = useState<UserSkill | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: '', description: '', content: '' });
  const [loading, setLoading] = useState(false);

  const load = () => {
    api.listUserSkills().then(setSkills).catch(() => {});
  };

  useEffect(() => { load(); }, []);

  const handleCreate = async () => {
    if (!form.name.trim()) return;
    setLoading(true);
    try {
      await api.createUserSkill(form);
      setCreating(false);
      setForm({ name: '', description: '', content: '' });
      load();
    } catch { /* keep form */ }
    setLoading(false);
  };

  const handleUpdate = async () => {
    if (!editing || !form.name.trim()) return;
    setLoading(true);
    try {
      await api.updateUserSkill(editing.id, form);
      setEditing(null);
      setForm({ name: '', description: '', content: '' });
      load();
    } catch { /* keep form */ }
    setLoading(false);
  };

  const handleDelete = async (id: number) => {
    if (!confirm('确定删除这个 Skill？')) return;
    await api.deleteUserSkill(id);
    load();
  };

  const startEdit = (skill: UserSkill) => {
    setEditing(skill);
    setCreating(false);
    setForm({ name: skill.name, description: skill.description, content: skill.content });
  };

  const startCreate = () => {
    setCreating(true);
    setEditing(null);
    setForm({ name: '', description: '', content: '' });
  };

  const cancel = () => {
    setCreating(false);
    setEditing(null);
    setForm({ name: '', description: '', content: '' });
  };

  const showForm = creating || editing;

  return (
    <div className="max-w-4xl mx-auto p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-foreground">Skills</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            自然语言知识/指导，创建后可在 Task 中选用，Agent 按需加载
          </p>
        </div>
        {!showForm && (
          <button
            onClick={startCreate}
            className="flex items-center gap-1 px-3 py-1.5 text-xs rounded bg-indigo-600 text-white hover:bg-indigo-500"
          >
            <Plus size={14} /> 新建 Skill
          </button>
        )}
      </div>

      {/* Create / Edit form */}
      {showForm && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-foreground">
              {creating ? '新建 Skill' : `编辑: ${editing!.name}`}
            </h3>
            <button onClick={cancel} className="text-gray-400 hover:text-foreground">
              <X size={16} />
            </button>
          </div>

          <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 items-center text-xs">
            <span className="text-gray-400">名称</span>
            <input
              className="bg-gray-700 text-foreground rounded px-2 py-1.5 text-xs border border-gray-600 focus:border-indigo-500 focus:outline-none"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="如: code-review-guide"
            />

            <span className="text-gray-400">描述</span>
            <input
              className="bg-gray-700 text-foreground rounded px-2 py-1.5 text-xs border border-gray-600 focus:border-indigo-500 focus:outline-none"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="一句话说明这个 skill 的用途"
            />

            <span className="text-gray-400 self-start mt-1">内容</span>
            <textarea
              className="bg-gray-700 text-foreground rounded px-2 py-1.5 text-xs border border-gray-600 focus:border-indigo-500 focus:outline-none min-h-[200px] font-mono"
              value={form.content}
              onChange={(e) => setForm({ ...form, content: e.target.value })}
              placeholder="Skill 的完整内容（Markdown），Agent 按需加载..."
            />
          </div>

          <div className="flex justify-end gap-2">
            <button onClick={cancel} className="px-3 py-1.5 text-xs rounded bg-gray-700 text-gray-300 hover:bg-gray-600">
              取消
            </button>
            <button
              onClick={creating ? handleCreate : handleUpdate}
              disabled={loading || !form.name.trim()}
              className="flex items-center gap-1 px-3 py-1.5 text-xs rounded bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              <Check size={13} /> {creating ? '创建' : '保存'}
            </button>
          </div>
        </div>
      )}

      {/* Skill list */}
      {skills.length === 0 && !showForm ? (
        <div className="text-center py-12 text-gray-500 text-sm">
          还没有 Skill，点击右上角创建一个
        </div>
      ) : (
        <div className="space-y-2">
          {skills.map((skill) => (
            <div
              key={skill.id}
              className={`bg-gray-800 border rounded-lg px-4 py-3 ${
                editing?.id === skill.id ? 'border-indigo-500' : 'border-gray-700'
              }`}
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-foreground">{skill.name}</span>
                    <span className="text-[10px] text-gray-600">
                      {skill.content.length} chars
                    </span>
                  </div>
                  {skill.description && (
                    <p className="text-xs text-gray-400 mt-0.5">{skill.description}</p>
                  )}
                  {skill.content && (
                    <pre className="text-[11px] text-gray-500 mt-1.5 max-h-20 overflow-hidden whitespace-pre-wrap">
                      {skill.content.slice(0, 200)}{skill.content.length > 200 ? '...' : ''}
                    </pre>
                  )}
                </div>
                <div className="flex items-center gap-1 ml-2 shrink-0">
                  <button
                    onClick={() => startEdit(skill)}
                    className="p-1.5 text-gray-500 hover:text-foreground transition-colors"
                    title="编辑"
                  >
                    <Pencil size={14} />
                  </button>
                  <button
                    onClick={() => handleDelete(skill.id)}
                    className="p-1.5 text-gray-500 hover:text-red-400 transition-colors"
                    title="删除"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
