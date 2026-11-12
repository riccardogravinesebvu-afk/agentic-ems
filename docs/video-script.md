# Video script (two minutes)

**[Screen: the meter tree in `docs/schema.md`]**

This is an invented factory. One site meter, four departments, ten lines, twenty-six machines. Every parent meter already contains its children. Ask for "the paint shop plus its line 2" and this benchmark counts the paint shop alone as right. Add the two and you count the same energy twice.

**[Screen: the three arms in the README]**

I asked one model fifty-five questions, five times each, three ways. First: it fills a structured request and a semantic layer writes the SQL. Second: it writes SQL itself after reading the rules. Third: it writes SQL with only the tables and the tree.

**[Screen: the results table]**

I expected the SQL arms to double count. With the rules in the prompt, the model never did.

Without the rules, its rows were sometimes wrong.

**[Screen: the H19 example, SQL and both values]**

Here is one. Department and line in the same sum. The returned row is a third too high. The model's text said so. Nothing in the row does.

**[Screen: the `out_of_scope` row of the semantic arm]**

The semantic layer was right inside its four metrics, bar one refusal. Outside them it refused, or returned the ingredients as rows while the model did the arithmetic in its text. My scorer reads rows, so those count as wrong.

**[Screen: `semantic.py`, the ancestor check]**

So the layer does not make this model more accurate. It makes a double-counted row impossible to return. The SQL arm avoided it by obeying a document.

**[Screen: the "Read this before quoting a number" list]**

One model, one day, synthetic data. The third arm was added after the first result, and the README says so. Every number regenerates from committed files.

Clone it and disagree with it.
