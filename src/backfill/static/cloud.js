window.backfillCloud = true;
window.cloudCommand = async (value) => {
  if (!value.command_id) return value;
  for (let attempt = 0; attempt < 12; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 700));
    const response = await fetch("/cloud/commands/" + value.command_id);
    if (response.status === 401) {
      location.replace("/");
      return value;
    }
    if (!response.ok) return value;
    const result = await response.json();
    if (!result.pending) {
      if (result.status >= 400)
        throw Error(
          result.body.error || "The machine could not apply this change",
        );
      return result.body;
    }
  }
  return value;
};
document.addEventListener("DOMContentLoaded", () => {
  const panel = document.querySelector("#cloud-machine");
  panel.className = "machine-bar";
  async function state() {
    try {
      const response = await fetch("/cloud/machine");
      if (response.status === 401) {
        location.replace("/");
        return;
      }
      if (!response.ok) return;
      const value = await response.json(),
        machine = value.machine;
      panel.replaceChildren();
      const info = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = machine
        ? `${machine.name} · ${machine.online ? "Online" : "Offline"}`
        : "Connect your machine";
      info.append(title);
      const detail = document.createElement("small");
      const pending = value.changes.filter((c) => c.status === null).length;
      const failed = value.changes.find((c) => c.status >= 400);
      detail.textContent = machine?.engine_error
        ? machine.engine_error
        : failed
          ? failed.error
          : pending
            ? `${pending} ${pending === 1 ? "change waiting" : "changes waiting"} for your machine`
            : !machine
              ? "Run your work on a machine that stays on."
              : !machine.online
                ? machine.seen
                  ? "Last seen " +
                    new Date(machine.seen * 1000).toLocaleString()
                  : "Waiting for its first connection"
                : "All changes synced.";
      info.append(detail);
      panel.append(info);
      const actions = document.createElement("div");
      actions.className = "machine-actions";
      const connect = document.createElement("button");
      connect.className = machine ? "quiet" : "primary";
      connect.textContent = machine ? "Disconnect" : "Connect machine";
      connect.onclick = async () => {
        if (machine) {
          if (
            !confirm(
              "Disconnect this machine? It will stop receiving new work.",
            )
          )
            return;
          await api("/cloud/machine/" + machine.id, "DELETE", {});
          await state();
          return;
        }
        const result = await api("/cloud/pairings", "POST", {});
        const dialog = document.createElement("dialog");
        dialog.className = "pair-dialog";
        const h = document.createElement("h2");
        h.textContent = "Connect your machine";
        const p = document.createElement("p");
        p.textContent =
          "Run this on your Linux machine. The code expires in 10 minutes.";
        const command = document.createElement("textarea");
        command.readOnly = true;
        command.value = result.command;
        command.rows = 4;
        command.setAttribute("aria-label", "Pairing command");
        const copy = document.createElement("button");
        copy.className = "primary";
        copy.textContent = "Copy command";
        copy.onclick = async () => {
          await navigator.clipboard.writeText(result.command);
          copy.textContent = "Copied";
        };
        const close = document.createElement("button");
        close.className = "quiet";
        close.textContent = "Done";
        close.onclick = () => {
          dialog.close();
          dialog.remove();
        };
        dialog.append(h, p, command, copy, close);
        document.body.append(dialog);
        dialog.showModal();
      };
      const logout = document.createElement("button");
      logout.className = "quiet";
      logout.textContent = "Sign out";
      logout.onclick = async () => {
        await api("/cloud/logout", "POST", {});
        location.replace("/");
      };
      actions.append(connect, logout);
      panel.append(actions);
    } catch {
      /* The existing dashboard connection banner handles network failures. */
    }
  }
  state();
  setInterval(state, 8000);
});

window.connectApplication = async (project) => {
 const dialog=document.createElement("dialog");dialog.className="pair-dialog";
 const heading=document.createElement("h2");heading.textContent="Connect application";
 const detail=document.createElement("p");detail.textContent="This connection shares only this project’s allowance.";
 const label=document.createElement("label");label.textContent="Application name";
 const name=document.createElement("input");name.placeholder="Task Finder";name.maxLength=100;label.append(name);
 const list=document.createElement("div"), error=document.createElement("p");error.setAttribute("role","alert");
 const command=document.createElement("textarea");command.readOnly=true;command.hidden=true;command.rows=4;command.setAttribute("aria-label","Application setup command");
 const copy=document.createElement("button");copy.textContent="Copy command";copy.hidden=true;
 copy.onclick=async()=>{await navigator.clipboard.writeText(command.value);copy.textContent="Copied";};
 const create=document.createElement("button");create.textContent="Create connection";create.className="primary";
 const base="/cloud/projects/"+encodeURIComponent(project)+"/connections";
 const refresh=async()=>{
  const rows=await api(base);list.replaceChildren();
  for(const item of rows.filter(r=>!r.revoked)){
   const row=document.createElement("p");row.textContent=item.name+" · "+(item.connected?"Connected":"Awaiting setup")+" ";
   const revoke=document.createElement("button");revoke.textContent="Disconnect";
   revoke.onclick=async()=>{try{await api(base+"/"+item.id,"DELETE",{});await refresh();}catch(e){error.textContent=e.message;}};
   row.append(revoke);list.append(row);
  }
 };
 create.onclick=async()=>{create.disabled=true;error.textContent="";try{
  const value=await api(base,"POST",{name:name.value.trim()||"Application"});command.value=value.command;command.hidden=false;copy.hidden=false;
  detail.textContent="Run this on the connected machine. The code expires in 10 minutes.";await refresh();
 }catch(e){error.textContent=e.message;}finally{create.disabled=false;}};
 const close=document.createElement("button");close.textContent="Close";close.onclick=()=>dialog.close();
 dialog.append(heading,detail,label,create,command,copy,list,error,close);document.body.append(dialog);
 dialog.addEventListener("close",()=>dialog.remove());dialog.showModal();
 try{await refresh();}catch(e){error.textContent=e.message;}
};
