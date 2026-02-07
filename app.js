const loginForm = document.querySelector("#loginForm");
const registerForm = document.querySelector("#registerForm");
const logoutButton = document.querySelector("#logoutButton");
const authCard = document.querySelector("#authCard");
const workspace = document.querySelector("#workspace");
const todoForm = document.querySelector("#todoForm");
const todoList = document.querySelector("#todoList");
const todoCount = document.querySelector("#todoCount");
const submitTodoButton = document.querySelector("#submitTodoButton");
const cancelEditButton = document.querySelector("#cancelEditButton");
const authTabs = document.querySelectorAll(".auth-tabs .tab");
const authPanels = document.querySelectorAll(".auth-panel");

const STORAGE_KEY = "easylife.todos";
const USER_KEY = "easylife.user";
const USERS_KEY = "easylife.users";

const state = {
  user: null,
  todos: [],
  activeId: null,
  editingId: null,
};

const formatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium",
  timeStyle: "short",
});

const loadTodos = () => {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return [];
  try {
    return JSON.parse(raw);
  } catch (error) {
    console.error("无法解析 Todo 数据", error);
    return [];
  }
};

const saveTodos = () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.todos));
};

const setUser = (username) => {
  state.user = username;
  localStorage.setItem(USER_KEY, username);
};

const getUser = () => localStorage.getItem(USER_KEY);

const loadUsers = () => {
  const raw = localStorage.getItem(USERS_KEY);
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch (error) {
    console.error("无法解析用户数据", error);
    return {};
  }
};

const saveUsers = (users) => {
  localStorage.setItem(USERS_KEY, JSON.stringify(users));
};

const showAuthTab = (targetId) => {
  authPanels.forEach((panel) => {
    panel.hidden = panel.id !== targetId;
  });
  authTabs.forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.target === targetId);
  });
};

const toggleAuthUI = (isLoggedIn) => {
  authCard.hidden = isLoggedIn;
  workspace.hidden = !isLoggedIn;
  logoutButton.hidden = !isLoggedIn;
};

const isLoggedIn = () => Boolean(state.user);

const ensureLoggedIn = () => {
  if (isLoggedIn()) return true;
  toggleAuthUI(false);
  return false;
};

const renderTodos = () => {
  todoList.innerHTML = "";
  todoCount.textContent = state.todos.length;

  if (!state.todos.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "还没有 Todo，创建你的第一个任务吧！";
    todoList.appendChild(empty);
    return;
  }

  state.todos.forEach((todo) => {
    const item = document.createElement("div");
    item.className = "todo-item";
    if (todo.id === state.activeId) {
      item.classList.add("active");
    }

    const title = document.createElement("strong");
    title.textContent = todo.title;

    const meta = document.createElement("div");
    meta.className = "todo-meta";
    const updatedMeta = todo.updatedAt
      ? ` · 更新于 ${formatter.format(new Date(todo.updatedAt))}`
      : "";
    meta.innerHTML = `<span>创建于 ${formatter.format(new Date(todo.createdAt))}${updatedMeta}</span><span>${todo.owner}</span>`;

    const actions = document.createElement("div");
    actions.className = "todo-actions";

    const selectButton = document.createElement("button");
    selectButton.type = "button";
    selectButton.textContent = "查看";
    selectButton.addEventListener("click", () => selectTodo(todo.id));

    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.className = "ghost";
    editButton.textContent = "编辑";
    editButton.addEventListener("click", (event) => {
      event.stopPropagation();
      startEdit(todo.id);
    });

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "delete";
    deleteButton.textContent = "删除";
    deleteButton.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteTodo(todo.id);
    });

    actions.append(selectButton, editButton, deleteButton);

    item.append(title, meta, actions);
    item.addEventListener("click", () => selectTodo(todo.id));

    todoList.appendChild(item);
  });
};

const selectTodo = (id) => {
  if (!ensureLoggedIn()) return;
  state.activeId = id;
  renderTodos();
};

const deleteTodo = (id) => {
  if (!ensureLoggedIn()) return;
  state.todos = state.todos.filter((todo) => todo.id !== id);
  if (state.activeId === id) {
    state.activeId = null;
  }
  if (state.editingId === id) {
    resetForm();
  }
  saveTodos();
  renderTodos();
};

const resetForm = () => {
  todoForm.reset();
  state.editingId = null;
  submitTodoButton.textContent = "新增 Todo";
  cancelEditButton.hidden = true;
};

const startEdit = (id) => {
  if (!ensureLoggedIn()) return;
  const todo = state.todos.find((item) => item.id === id);
  if (!todo) return;
  state.editingId = id;
  todoForm.elements.title.value = todo.title;
  todoForm.elements.content.value = todo.content;
  submitTodoButton.textContent = "保存修改";
  cancelEditButton.hidden = false;
};

loginForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const data = new FormData(loginForm);
  const username = data.get("username").trim();
  const password = data.get("password").trim();
  if (!username || !password) return;
  const users = loadUsers();
  if (!users[username] || users[username] !== password) {
    alert("用户名或密码错误，请重试。");
    return;
  }
  setUser(username);
  toggleAuthUI(true);
  loginForm.reset();
  renderTodos();
});

registerForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const data = new FormData(registerForm);
  const username = data.get("username").trim();
  const password = data.get("password").trim();
  const confirmPassword = data.get("confirmPassword").trim();
  if (!username || !password) return;
  if (password !== confirmPassword) {
    alert("两次输入的密码不一致，请检查。");
    return;
  }
  const users = loadUsers();
  if (users[username]) {
    alert("用户名已存在，请更换。");
    return;
  }
  users[username] = password;
  saveUsers(users);
  setUser(username);
  toggleAuthUI(true);
  registerForm.reset();
  renderTodos();
});

logoutButton.addEventListener("click", () => {
  localStorage.removeItem(USER_KEY);
  state.user = null;
  state.activeId = null;
  resetForm();
  toggleAuthUI(false);
});

todoForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!ensureLoggedIn()) return;
  const data = new FormData(todoForm);
  const title = data.get("title").trim();
  const content = data.get("content").trim();
  if (!title || !content) return;

  if (state.editingId) {
    const existing = state.todos.find((todo) => todo.id === state.editingId);
    if (!existing) return;
    existing.title = title;
    existing.content = content;
    existing.updatedAt = new Date().toISOString();
    state.activeId = existing.id;
  } else {
    const newTodo = {
      id: crypto.randomUUID(),
      title,
      content,
      owner: state.user,
      createdAt: new Date().toISOString(),
      updatedAt: null,
    };

    state.todos.unshift(newTodo);
    state.activeId = newTodo.id;
  }
  saveTodos();
  resetForm();
  renderTodos();
});

cancelEditButton.addEventListener("click", () => {
  resetForm();
});

const init = () => {
  state.todos = loadTodos();
  const savedUser = getUser();
  if (savedUser) {
    state.user = savedUser;
    toggleAuthUI(true);
  } else {
    toggleAuthUI(false);
  }
  authTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      showAuthTab(tab.dataset.target);
    });
  });
  showAuthTab("loginPanel");
  renderTodos();
};

init();
