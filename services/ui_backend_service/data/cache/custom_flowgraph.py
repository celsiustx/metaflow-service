import ast


class DAGNode(object):
    def __init__(self, func_ast, decos, doc):
        self.name = func_ast.name
        self.func_lineno = func_ast.lineno
        self.decorators = decos
        self.doc = doc.rstrip()

        # these attributes are populated by _parse
        self.tail_next_lineno = 0
        self.type = None
        self.out_funcs = []
        self.has_tail_next = False
        self.invalid_tail_next = False
        self.num_args = 0
        self.condition = None
        self.foreach_param = None
        self._parse(func_ast)

        # these attributes are populated by _traverse_graph
        self.in_funcs = set()
        self.split_parents = []
        self.matching_join = None

        # these attributes are populated by _postprocess
        self.is_inside_foreach = False

    def _expr_str(self, expr):
        return '%s.%s' % (expr.value.id, expr.attr)

    def _parse(self, func_ast):

        self.num_args = len(func_ast.args.args)
        tail = func_ast.body[-1]

        # end doesn't need a transition
        if self.name == 'end':
            # TYPE: end
            self.type = 'end'

        # ensure that the tail an expression
        if not isinstance(tail, ast.Expr):
            return

        # determine the type of self.next transition
        try:
            if not self._expr_str(tail.value.func) == 'self.next':
                return

            self.has_tail_next = True
            self.invalid_tail_next = True
            self.tail_next_lineno = tail.lineno
            self.out_funcs = [e.attr for e in tail.value.args]
            keywords = dict((k.arg, k.value.s) for k in tail.value.keywords)

            if len(keywords) == 1:
                if 'foreach' in keywords:
                    # TYPE: foreach
                    self.type = 'foreach'
                    if len(self.out_funcs) == 1:
                        self.foreach_param = keywords['foreach']
                        self.invalid_tail_next = False
                elif 'condition' in keywords:
                    # TYPE: split-or
                    self.type = 'split-or'
                    if len(self.out_funcs) == 2:
                        self.condition = keywords['condition']
                        self.invalid_tail_next = False
            elif len(keywords) == 0:
                if len(self.out_funcs) > 1:
                    # TYPE: split-and
                    self.type = 'split-and'
                    self.invalid_tail_next = False
                elif len(self.out_funcs) == 1:
                    # TYPE: linear
                    if self.num_args > 1:
                        self.type = 'join'
                    else:
                        self.type = 'linear'
                    self.invalid_tail_next = False

        except AttributeError:
            return

    def __str__(self):
        return """
            *[{0.name} {0.type} (line {0.func_lineno})]*
            in_funcs={in_funcs}
            split_parents={parents}
            matching_join={matching_join}
            is_inside_foreach={is_inside_foreach}
            decorators={decos}
            num_args={0.num_args}
            has_tail_next={0.has_tail_next} (line {0.tail_next_lineno})
            invalid_tail_next={0.invalid_tail_next}
            condition={0.condition}
            foreach_param={0.foreach_param}
            -> {out}"""\
            .format(self,
                    matching_join=self.matching_join and '[%s]' % self.matching_join,
                    is_inside_foreach=self.is_inside_foreach,
                    in_funcs=', '.join('[%s]' % x for x in self.in_funcs),
                    parents=', '.join('[%s]' % x for x in self.split_parents),
                    decos=' | '.join(map(str, self.decorators)),
                    out=', '.join('[%s]' % x for x in self.out_funcs))


class StepVisitor(ast.NodeVisitor):

    def __init__(self, nodes):
        self.nodes = nodes
        super(StepVisitor, self).__init__()

    def visit_FunctionDef(self, node):
        decos = [d.func.id if isinstance(d, ast.Call) else d.id
                 for d in node.decorator_list]
        if 'step' in decos:
            doc = ast.get_docstring(node)
            self.nodes[node.name] = DAGNode(node, decos, doc if doc else '')


class FlowGraph(object):
    # NOTE: This implementation relies on passing in the name of the FlowSpec class
    # to be parsed from the sourcecode.
    def __init__(self, source, name):
        self.name = name

        self.nodes = self._create_nodes(source)
        self._traverse_graph()
        self._postprocess()

    def _create_nodes(self, source):
        def _flow(n):
            if isinstance(n, ast.ClassDef):
                return n.name == self.name

        # NOTE: Can possibly fail if filter returns multiple results,
        # but this would mean there are duplicate class names.
        [root] = list(filter(_flow, ast.parse(source).body))
        self.name = root.name
        doc = ast.get_docstring(root)
        self.doc = doc if doc else ''
        nodes = {}
        StepVisitor(nodes).visit(root)
        return nodes

    def _postprocess(self):
        # any node who has a foreach as any of its split parents
        # has is_inside_foreach=True *unless* all of those foreaches
        # are joined by the node
        for node in self.nodes.values():
            foreaches = [p for p in node.split_parents
                         if self.nodes[p].type == 'foreach']
            if [f for f in foreaches
                    if self.nodes[f].matching_join != node.name]:
                node.is_inside_foreach = True

    def _traverse_graph(self):

        def traverse(node, seen, split_parents):

            if node.type in ('split-or', 'split-and', 'foreach'):
                node.split_parents = split_parents
                split_parents = split_parents + [node.name]
            elif node.type == 'join':
                # ignore joins without splits
                if split_parents:
                    self[split_parents[-1]].matching_join = node.name
                    node.split_parents = split_parents
                    split_parents = split_parents[:-1]
            else:
                node.split_parents = split_parents

            for n in node.out_funcs:
                # graph may contain loops - ignore them
                if n not in seen:
                    # graph may contain unknown transitions - ignore them
                    if n in self:
                        child = self[n]
                        child.in_funcs.add(node.name)
                        traverse(child, seen + [n], split_parents)

        if 'start' in self:
            traverse(self['start'], [], [])

        # fix the order of in_funcs
        for node in self.nodes.values():
            node.in_funcs = sorted(node.in_funcs)

    def __getitem__(self, x):
        return self.nodes[x]

    def __contains__(self, x):
        return x in self.nodes

    def __iter__(self):
        return iter(self.nodes.values())

    def __str__(self):
        return '\n'.join(str(n) for _, n in sorted((n.func_lineno, n)
                                                   for n in self.nodes.values()))

    def output_steps(self):

        steps_info = {}
        graph_structure = []

        def node_to_type(node):
            "convert internal node type to a more descriptive one for API consumers."
            if node.type in ["linear", "start", "end", "join"]:
                return node.type
            elif node.type == "split":
                return "split-static"
            elif node.type == "foreach":
                if node.parallel_foreach:
                    return "split-parallel"
                return "split-foreach"
            return "unknown"  # Should never happen

        def node_to_dict(name, node):
            return {
                "name": name,
                "type": node_to_type(node),
                "line": node.func_lineno,
                "doc": node.doc,
                "next": node.out_funcs,
                "foreach_artifact": node.foreach_param,
            }

        def populate_block(start_name, end_name):
            cur_name = start_name
            resulting_list = []
            while cur_name != end_name:
                cur_node = self.nodes[cur_name]
                node_dict = node_to_dict(cur_name, cur_node)

                steps_info[cur_name] = node_dict
                resulting_list.append(cur_name)

                if cur_node.type not in ("start", "linear", "join"):
                    # We need to look at the different branches for this
                    resulting_list.append(
                        [
                            populate_block(s, cur_node.matching_join)
                            for s in cur_node.out_funcs
                        ]
                    )
                    cur_name = cur_node.matching_join
                else:
                    cur_name = cur_node.out_funcs[0]
            return resulting_list

        graph_structure = populate_block("start", "end")

        steps_info["end"] = node_to_dict("end", self.nodes["end"])
        graph_structure.append("end")

        return steps_info, graph_structure
