import { useEffect, useState } from "react";
import { api } from "@/api";
import type { PluginList } from "@/types";
import { Checkbox } from "@/components/ui/checkbox";

/** Checkboxes for the apps this agent may use. Null means every connected plugin. */
export default function PluginChoices({
  value,
  onChange,
}: {
  value: string[] | null;
  onChange: (plugins: string[] | null) => void;
}) {
  const [list, setList] = useState<PluginList | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let stopped = false;
    api<PluginList>("GET", "/api/plugins")
      .then((loaded) => {
        if (!stopped) setList(loaded);
      })
      .catch((err: Error) => {
        if (!stopped) setError(err.message);
      });
    return () => {
      stopped = true;
    };
  }, []);

  const connected = (list?.plugins ?? []).filter((plugin) => plugin.connected);
  const checked = new Set(value === null ? connected.map((plugin) => plugin.slug) : value);

  const toggle = (slug: string, on: boolean) => {
    const next = new Set(value === null ? connected.map((plugin) => plugin.slug) : value);
    if (on) next.add(slug);
    else next.delete(slug);
    const slugs = connected.map((plugin) => plugin.slug);
    const all = slugs.length > 0 && slugs.every((item) => next.has(item)) && [...next].every((item) => slugs.includes(item));
    onChange(all ? null : [...next]);
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="text-sm font-bold">Plugins</div>
      {error && <div className="text-destructive text-[13px]">{error}</div>}
      {!list && !error && <div className="text-muted-foreground text-[13px]">Loading plugins…</div>}
      {list && !list.configured && (
        <p className="text-muted-foreground text-[13px]">
          Connect apps from Plugins in the sidebar, then choose which ones this agent can use.
        </p>
      )}
      {list?.configured && connected.length === 0 && (
        <p className="text-muted-foreground text-[13px]">
          No apps are connected yet. Connect them from Plugins in the sidebar.
          {value === null ? " This agent can use apps you connect later." : " This agent has no connected app access until you choose apps here."}
        </p>
      )}
      {connected.length > 0 && (
        <>
          <p className="text-muted-foreground text-[13px]">
            Leave them all selected and this agent can use every connected app, including ones added later.
          </p>
          <div className="flex flex-col gap-1.5">
            {connected.map((plugin) => (
              <label key={plugin.slug} className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={checked.has(plugin.slug)}
                  onCheckedChange={(next) => toggle(plugin.slug, next === true)}
                />
                {plugin.name}
              </label>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
