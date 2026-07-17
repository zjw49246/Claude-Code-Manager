import { api } from '../../api/client';
import type { Task } from '../../api/client';
import { Check, X } from '../icons';

interface PlanPanelProps {
  tasks: Task[];
  onRefresh: () => void;
}

export function PlanPanel({ tasks, onRefresh }: PlanPanelProps) {
  const planTasks = tasks.filter((t) => t.status === 'plan_review' && t.plan_content);

  if (planTasks.length === 0) return null;

  const handleApprove = async (id: number) => {
    await api.approvePlan(id);
    onRefresh();
  };

  const handleReject = async (id: number) => {
    await api.rejectPlan(id);
    onRefresh();
  };

  return (
    <div className="space-y-3">
      <h2 className="text-foreground font-semibold flex items-center gap-2">
        Plans Awaiting Review
        <span className="bg-yellow-500 text-black text-xs px-2 py-0.5 rounded-full font-bold">{planTasks.length}</span>
      </h2>
      {planTasks.map((task) => (
        <div key={task.id} className="bg-gray-800 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <span className="text-foreground font-medium text-sm">{task.title}</span>
              <span className="text-gray-500 text-xs ml-2">#{task.id}</span>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => handleApprove(task.id)}
                className="flex items-center gap-1 bg-green-600 hover:bg-green-700 text-white px-3 py-1.5 rounded text-xs font-medium"
              >
                <Check size={14} /> Approve
              </button>
              <button
                onClick={() => handleReject(task.id)}
                className="flex items-center gap-1 bg-red-600 hover:bg-red-700 text-white px-3 py-1.5 rounded text-xs font-medium"
              >
                <X size={14} /> Reject
              </button>
            </div>
          </div>
          <div className="text-xs text-gray-400 bg-gray-900 rounded p-3 max-h-60 overflow-y-auto whitespace-pre-wrap font-mono">
            {task.plan_content}
          </div>
        </div>
      ))}
    </div>
  );
}
