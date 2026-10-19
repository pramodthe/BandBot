import type { ComponentType, SVGProps } from "react";
import { BlocksIcon, LayersIcon, LogOutIcon, MessageCircleIcon, SearchIcon, SquarePenIcon, UsersIcon, SettingsIcon } from "lucide-react";
import type { State } from "@/store";
import { isAdmin } from "@/store";
import Avatar from "@/components/shared/Avatar";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Modal, WorkspaceView } from "./types";
import ThemeToggle from "./ThemeToggle";
import { cn } from "@/lib/utils";

function RailButton({ label, icon: Icon, active, onClick }: {
  label: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="rail" size="icon-lg" aria-label={label} aria-pressed={active} onClick={onClick}><Icon /></Button>
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

export default function WorkspaceNavigation({ state, view, onView, onSearch, openModal, onSignOut, detailOpen = false }: {
  state: State;
  view: WorkspaceView;
  onView: (view: WorkspaceView) => void;
  onSearch: () => void;
  openModal: (modal: Modal) => void;
  onSignOut: () => void;
  detailOpen?: boolean;
}) {
  return (
    <nav aria-label="Workspace navigation" className={cn("border-border bg-sidebar flex w-[52px] shrink-0 flex-col items-center border-r py-3", detailOpen && "max-sm:hidden")}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon-lg" className="text-primary mb-2" aria-label="BAND_BOTS workspace" onClick={() => onView("members")}><LayersIcon className="size-[22px]" strokeWidth={2.5} /></Button>
        </TooltipTrigger>
        <TooltipContent side="right">BAND_BOTS</TooltipContent>
      </Tooltip>
      <div className="flex flex-col gap-1">
        <RailButton label="New channel" icon={SquarePenIcon} onClick={() => openModal({ kind: "new-channel" })} />
        <RailButton label={view === "chat" ? "Find a conversation" : "Find a member"} icon={SearchIcon} onClick={onSearch} />
        <Separator className="mx-auto my-1 w-6" />
        <RailButton label="Chat" icon={MessageCircleIcon} active={view === "chat"} onClick={() => onView("chat")} />
        <RailButton label="Members" icon={UsersIcon} active={view === "members"} onClick={() => onView("members")} />
        <Separator className="mx-auto my-1 w-6" />
        <RailButton label="Connected apps" icon={BlocksIcon} onClick={() => openModal({ kind: "plugins" })} />
        {isAdmin(state) && <RailButton label="OpenAI settings" icon={SettingsIcon} onClick={() => openModal({ kind: "openai" })} />}
      </div>
      <div className="mt-auto flex flex-col items-center gap-2 pt-4">
        <ThemeToggle />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-lg" aria-label="Open user menu"><Avatar name={state.me.display_name} kind="human" size={26} /></Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="right" align="end" sideOffset={10} className="min-w-52">
            <DropdownMenuLabel>{state.me.display_name}</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              {isAdmin(state) && state.activeChannelId !== null && <DropdownMenuItem onSelect={() => openModal({ kind: "elements" })}><BlocksIcon />Workspace interfaces</DropdownMenuItem>}
              <DropdownMenuItem onSelect={onSignOut}><LogOutIcon />Sign out</DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </nav>
  );
}
