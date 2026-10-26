import { useState, type ComponentType, type FormEvent, type SVGProps } from "react";
import { BlocksIcon, LogOutIcon, MessageCircleIcon, SearchIcon, SquarePenIcon, UsersIcon, UserPlusIcon, SettingsIcon } from "lucide-react";
import { api } from "@/api";
import type { State } from "@/store";
import { isAdmin } from "@/store";
import Avatar from "@/components/shared/Avatar";
import { BrandMark } from "@/components/shared/Brand";
import Field from "@/components/shared/Field";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Modal, WorkspaceView } from "./types";
import ThemeToggle from "./ThemeToggle";
import { cn } from "@/lib/utils";

function RailButton({ label, caption, icon: Icon, active, badge, onClick }: {
  label: string;
  caption?: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  active?: boolean;
  badge?: number;
  onClick: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="rail" size="icon-lg" className={cn("relative", active && "aria-pressed:bg-brand-soft aria-pressed:text-brand-soft-foreground", caption && "h-12 w-11 flex-col gap-1 rounded-xl text-[9px] leading-none [&_svg]:size-[18px]")} aria-label={label} aria-pressed={active} onClick={onClick}>
          <Icon />{caption && <span>{caption}</span>}
          {badge ? <span className="bg-primary text-primary-foreground absolute -top-1 -right-1 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] font-bold">{badge > 9 ? "9+" : badge}</span> : null}
        </Button>
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
  const pendingPeople = state.invitations.length + state.contactRequests.length;
  const [recoveryOpen, setRecoveryOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [recoveryNotice, setRecoveryNotice] = useState("");
  const [recoveryError, setRecoveryError] = useState("");
  const leave = async (everywhere: boolean) => {
    try { await api("POST", everywhere ? "/api/auth/logout-all" : "/api/auth/logout"); }
    catch { /* A revoked session still leaves the browser signed out. */ }
    onSignOut();
  };
  const saveRecovery = async (event: FormEvent) => {
    event.preventDefault();
    setRecoveryError("");
    setRecoveryNotice("");
    try {
      if (email.trim()) await api("POST", "/api/account/recovery-email", { email: email.trim() });
      if (code.trim()) await api("POST", "/api/auth/recovery/verify", { token: code.trim() });
      setRecoveryNotice(code.trim() ? "Recovery email verified." : "Verification code sent. Enter it here after it arrives.");
      if (code.trim()) setCode("");
    } catch (cause) {
      setRecoveryError((cause as Error).message);
    }
  };
  return (
    <nav aria-label="Workspace navigation" className={cn("border-border bg-sidebar flex w-[52px] shrink-0 flex-col items-center border-r py-3", detailOpen && "max-sm:hidden")}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon-lg" className="mb-4 p-1" aria-label="Band Bots workspace" onClick={() => onView("chat")}><BrandMark className="size-8" /></Button>
        </TooltipTrigger>
        <TooltipContent side="right">Band Bots · Your workspace</TooltipContent>
      </Tooltip>
      <div className="flex flex-col gap-1">
        <RailButton label="New channel" icon={SquarePenIcon} onClick={() => openModal({ kind: "new-channel" })} />
        <RailButton label={view === "chat" ? "Find a conversation" : "Find a member"} icon={SearchIcon} onClick={onSearch} />
        <Separator className="mx-auto my-1 w-6" />
        <RailButton label="Chat" caption="Chat" icon={MessageCircleIcon} active={view === "chat"} onClick={() => onView("chat")} />
        <RailButton label="Members" caption="Team" icon={UsersIcon} active={view === "members"} onClick={() => onView("members")} />
        <RailButton label={pendingPeople ? `People, ${pendingPeople} pending` : "People"} icon={UserPlusIcon} badge={pendingPeople} onClick={() => openModal({ kind: "people" })} />
        <Separator className="mx-auto my-1 w-6" />
        <RailButton label="Connected apps" caption="Apps" icon={BlocksIcon} onClick={() => openModal({ kind: "plugins" })} />
        {isAdmin(state) && <RailButton label="Providers & APIs" icon={SettingsIcon} onClick={() => openModal({ kind: "providers" })} />}
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
              <DropdownMenuItem onSelect={() => openModal({ kind: "people" })}><UserPlusIcon />People</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setRecoveryOpen(true)}>Recovery email</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => void leave(false)}><LogOutIcon />Sign out</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => void leave(true)}>Sign out everywhere</DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
        <Dialog open={recoveryOpen} onOpenChange={setRecoveryOpen}>
          <DialogContent className="rounded-2xl sm:max-w-[420px]">
            <DialogHeader className="text-left">
              <DialogTitle>Recovery email</DialogTitle>
              <DialogDescription>Verify an email before it can reset your password. The reset code works once.</DialogDescription>
            </DialogHeader>
            <form className="space-y-3" onSubmit={(event) => void saveRecovery(event)}>
              <Field id="recovery-email" label="Email">
                <Input id="recovery-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" required />
              </Field>
              <Field id="recovery-code" label="Verification code" hint="(after it arrives)">
                <Input id="recovery-code" value={code} onChange={(event) => setCode(event.target.value)} autoComplete="one-time-code" />
              </Field>
              {recoveryNotice && <p className="text-sm" role="status">{recoveryNotice}</p>}
              {recoveryError && <p className="text-destructive text-sm" role="alert">{recoveryError}</p>}
              <Button type="submit">Save recovery email</Button>
            </form>
          </DialogContent>
        </Dialog>
      </div>
    </nav>
  );
}
