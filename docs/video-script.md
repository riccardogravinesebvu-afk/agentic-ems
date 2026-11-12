# Video script (two minutes)

**[Screen: the meter tree in `docs/schema.md`]**

This is an invented factory. One site meter, four departments, ten lines, twenty-six machines. Every parent meter already contains its children. Ask for "the paint shop plus its line 2" and the right answer is the paint shop alone. Add the two and you count the same energy twice.

**[Screen: the three arms in the README]**

I asked one model fifty-five questions, five times each, three ways. First: it fills a structured request and a semantic layer writes the SQL. Second: it writes SQL itself after reading the rules. Third: it writes SQL with only the tables and the tree.

**[Screen: the results table]**

I expected the SQL arms to double count. With the rules in the prompt, the model never did.

Without the rules it did, a handful of times.

**[Screen: the H19 example, SQL and both values]**

Here is one. Department and line in the same sum. The number looks fine. It is a third too high, and nothing warns you.

**[Screen: the `out_of_scope` row of the semantic arm]**

The semantic layer was right on everything it defines. Outside that, it refused, or it returned the ingredients as rows and the model did the arithmetic in its text. My scorer reads rows, so those count as wrong.

**[Screen: `semantic.py`, the ancestor check]**

So the layer does not make this model more accurate. It makes one mistake impossible to build. The SQL arm avoided it by obeying a document.

**[Screen: the "Read this before quoting a number" list]**

One model, one day, synthetic data. The third arm was added after the first result, and the README says so. Every number regenerates from committed files, and every run has its transcript.

Clone it, run it, disagree with it.
