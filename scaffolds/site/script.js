const toggle = document.querySelector(".nav-toggle");
const nav = document.getElementById("nav");

toggle.addEventListener("click", () => {
  const open = nav.classList.toggle("open");
  toggle.setAttribute("aria-expanded", String(open));
});

nav.addEventListener("click", (event) => {
  if (event.target.closest("a")) {
    nav.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  }
});

document.getElementById("year").textContent = new Date().getFullYear();
