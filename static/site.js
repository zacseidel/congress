// Click a header to sort its table.
document.querySelectorAll("table.sortable").forEach(function (table) {
  table.querySelectorAll("th").forEach(function (header, index) {
    header.addEventListener("click", function () {
      var body = table.tBodies[0];
      var rows = Array.prototype.slice.call(body.rows);
      var ascending = header.dataset.asc !== "true";
      header.dataset.asc = ascending;
      rows.sort(function (a, b) {
        var x = a.cells[index], y = b.cells[index];
        var nx = parseFloat((x.dataset.v || x.textContent).replace(/[^0-9.\-]/g, ""));
        var ny = parseFloat((y.dataset.v || y.textContent).replace(/[^0-9.\-]/g, ""));
        if (!isNaN(nx) && !isNaN(ny)) return ascending ? nx - ny : ny - nx;
        return ascending
          ? x.textContent.localeCompare(y.textContent)
          : y.textContent.localeCompare(x.textContent);
      });
      rows.forEach(function (row) { body.appendChild(row); });
    });
  });
});
