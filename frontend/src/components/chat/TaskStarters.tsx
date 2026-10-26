import { ArrowUpRightIcon, CodeIcon, ListChecksIcon, SearchIcon } from "lucide-react";

const STARTERS = [
  { title: "Explore an idea", description: "Turn a question into a research brief", icon: SearchIcon, prompt: "Help me prepare a research brief. First ask me what topic, audience, and decision it should support." },
  { title: "Make a plan", description: "Find the next steps, together", icon: ListChecksIcon, prompt: "Help me plan a product launch. First ask me about the product, audience, timeline, and goals, then propose a plan with owners and next steps." },
  { title: "Build something", description: "Take an idea to a working app", icon: CodeIcon, prompt: "Help me build a small web app. First clarify what it should do and who it is for, then propose a plan before building." },
];

export default function TaskStarters({ agentName, isDm, onPick }: { agentName: string; isDm: boolean; onPick: (text: string) => void }) {
  return (
    <div className="mt-4 w-full max-w-2xl text-left">
      <p className="text-muted-foreground mb-3 text-center text-xs">Pick a starting point. Make it yours before sending.</p>
      <div className="grid gap-2 min-[1100px]:grid-cols-3">
        {STARTERS.map(({ title, description, icon: Icon, prompt }) => (
          <button key={title} type="button" onClick={() => onPick(`${isDm ? "" : `@${agentName} `}${prompt}`)} className="group border-border bg-background hover:border-primary/40 hover:bg-brand-soft/50 flex min-w-0 items-center gap-3 rounded-2xl border p-4 text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring min-[1100px]:flex-col min-[1100px]:items-start">
            <span className="text-brand-soft-foreground bg-brand-soft flex size-9 shrink-0 items-center justify-center rounded-xl"><Icon className="size-4" /></span>
            <span className="min-w-0 flex-1"><span className="flex items-center justify-between gap-2 text-sm font-medium">{title}<ArrowUpRightIcon className="text-muted-foreground size-3.5" /></span><span className="text-muted-foreground mt-1 block text-xs leading-relaxed">{description}</span></span>
          </button>
        ))}
      </div>
    </div>
  );
}
