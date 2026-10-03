({ kind, url }) => {
  const state = window.__INITIAL_STATE__ || {};
  const raw = (value) => value?._rawValue ?? value?.value ?? value;
  const makeNoteUrl = (id, token) => {
    if (!id || !token) return null;
    return `https://www.xiaohongshu.com/discovery/item/${id}?source=webshare&xhsshare=pc_web&xsec_token=${token}&xsec_source=pc_share`;
  };
  const normalizeRows = (rows) => {
    if (!Array.isArray(rows)) return [];
    return rows.map((item) => {
      const card = item?.noteCard || item?.note_card || item || {};
      const user = card?.user || item?.user || {};
      const id = item?.id || item?.noteId || item?.note_id || card?.id || card?.noteId || card?.note_id;
      const token = item?.xsecToken || item?.xsec_token || card?.xsecToken || card?.xsec_token;
      const noteUrl = makeNoteUrl(id, token);
      if (!noteUrl) return null;
      return {
        id,
        token,
        url: noteUrl,
        title: card?.displayTitle || card?.title || item?.displayTitle || item?.title || "",
        author: user?.nickName || user?.nickname || user?.name || "",
      };
    }).filter(Boolean);
  };

  let stateNotes = [];
  if (kind === "profile_liked") {
    stateNotes = normalizeRows(raw(state.user?.notes)?.[2]);
  } else if (kind === "profile_saved") {
    stateNotes = normalizeRows(raw(state.user?.notes)?.[1]);
  } else if (kind === "profile_published") {
    stateNotes = normalizeRows(raw(state.user?.notes)?.[0]);
  } else {
    const userNotes = raw(state.user?.notes);
    if (Array.isArray(userNotes)) {
      userNotes.forEach((rows) => stateNotes.push(...normalizeRows(rows)));
    }
  }

  const hrefNotes = Array.from(document.querySelectorAll("a[href]")).map((a) => {
    try {
      const link = new URL(a.getAttribute("href"), location.href);
      if (!/xiaohongshu\.com$/.test(link.hostname)) return null;
      if (!(/\/explore\/|\/discovery\/item\//.test(link.pathname))) return null;
      return {
        id: link.pathname.split("/").filter(Boolean).pop(),
        url: link.href,
        title: "",
        author: "",
      };
    } catch {
      return null;
    }
  }).filter(Boolean);

  return { stateNotes, hrefNotes };
}
